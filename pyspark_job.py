# =============================================================================
#  CISC 886 – Cloud Computing | Queen's University
#  Horus-OSINT: Data Preprocessing Pipeline (AWS EMR / PySpark)
#  Student NetID : 25bbdf-g23
#
#  Description:
#    Reads raw GTD data from project S3, reads GDELT V1 historical exports
#    directly from the AWS Open Data Registry, performs a distributed JOIN,
#    applies the Meta-Llama-3 instruct conversational template per record,
#    and writes train / val / test splits back to S3 as JSONL.
#
#  EMR Step arguments:
#    --bucket  horus-25bbdf-g23-bucket
#    --gtd_key raw/gtd/gtd_merged.csv        (default shown)
#    --gdelt_years 2015,2016,2017,2018,2019  (default shown)
#    --max_records 200000                    (safety cap, 0 = no cap)
#
#  Usage (local test):
#    spark-submit pyspark_job.py --bucket horus-25bbdf-g23-bucket
# =============================================================================

import sys
import json
import argparse
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType

# ---------------------------------------------------------------------------
# 0. Argument Parsing — required by EMR Step "Arguments" field
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Horus-OSINT PySpark Preprocessing")
parser.add_argument("--bucket",       default="horus-25bbdf-g23-bucket",
                    help="S3 bucket name (no s3:// prefix)")
parser.add_argument("--gtd_key",      default="raw/gtd/gtd_merged.csv",
                    help="S3 key for the merged GTD CSV")
parser.add_argument("--gdelt_years",  default="2015,2016,2017,2018,2019",
                    help="Comma-separated list of GDELT years to ingest")
parser.add_argument("--max_records",  type=int, default=200_000,
                    help="Max GTD records to process (0 = no cap). Prevents OOM.")
# parse_known_args absorbs any extra flags EMR injects (e.g. --conf)
args, _ = parser.parse_known_args()

NETID          = "25bbdf-g23"
S3_BUCKET      = f"s3://{args.bucket}"
GTD_RAW_PATH   = f"{S3_BUCKET}/{args.gtd_key}"
OUTPUT_PATH    = f"{S3_BUCKET}/processed"
GDELT_YEARS    = [y.strip() for y in args.gdelt_years.split(",")]

# GDELT V1 historical exports live at this public bucket in us-east-1.
# Each yearly glob expands to ~365 daily .export.CSV files (~1-4 GB/year).
GDELT_GLOBS    = [
    f"s3://gdelt-open-data/events/{year}*.export.CSV"
    for year in GDELT_YEARS
]

print(f"[CONFIG] Bucket    : {S3_BUCKET}")
print(f"[CONFIG] GTD source: {GTD_RAW_PATH}")
print(f"[CONFIG] GDELT glob: {GDELT_GLOBS}")
print(f"[CONFIG] Output    : {OUTPUT_PATH}")

# ---------------------------------------------------------------------------
# 1. Spark Session — tuned for m5.xlarge (4 vCPU, 16 GB) × 2 core nodes
# ---------------------------------------------------------------------------
spark = (
    SparkSession.builder
    .appName(f"{NETID}-HorusOSINT-DataPrep")
    # Reduce shuffle partitions from 200 → 100; our data fits in memory
    .config("spark.sql.shuffle.partitions", "100")
    # Adaptive query execution — auto-coalesces skewed partitions
    .config("spark.sql.adaptive.enabled", "true")
    .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
    # Push GTD CSV schema inference off the critical path
    .config("spark.sql.legacy.timeParserPolicy", "LEGACY")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")
print("[*] Spark Session started.")

# ---------------------------------------------------------------------------
# 2. Ingest GTD Data
#    Key GTD V1 columns we need (explicit schema avoids full-scan inferSchema)
# ---------------------------------------------------------------------------
GTD_COLUMNS = {
    "iyear":            "year",
    "country_txt":      "country",
    "attacktype1_txt":  "attack_type",
    "targtype1_txt":    "target_type",
    "weaptype1_txt":    "weapon_type",
    "nkill":            "kills",
    "nwound":           "wounds",
    "gname":            "group_name",
    "summary":          "summary",
}

try:
    # inferSchema=False + castng is faster on large CSVs than inferSchema=True
    gtd_raw = (
        spark.read
        .option("header", "true")
        .option("inferSchema", "false")
        .option("multiLine", "true")   # GTD summaries contain embedded newlines
        .option("escape", '"')
        .csv(GTD_RAW_PATH)
    )

    # Select only the columns we care about; rename for clarity
    select_exprs = [
        F.col(src).alias(dst)
        for src, dst in GTD_COLUMNS.items()
        if src in gtd_raw.columns
    ]
    gtd_df = gtd_raw.select(select_exprs)

    # Cast numeric columns; fill missing casualties with 0
    gtd_df = (
        gtd_df
        .withColumn("year",   F.col("year").cast("int"))
        .withColumn("kills",  F.coalesce(F.col("kills").cast("double"),  F.lit(0.0)))
        .withColumn("wounds", F.coalesce(F.col("wounds").cast("double"), F.lit(0.0)))
        .dropna(subset=["year", "country", "attack_type"])
        .filter(F.col("attack_type") != "Unknown")
    )

    # Optional record cap — prevents runaway costs during dev/test
    if args.max_records > 0:
        gtd_df = gtd_df.limit(args.max_records)

    gtd_count = gtd_df.cache().count()
    print(f"[GTD] Loaded {gtd_count:,} records after cleaning.")

except Exception as exc:
    print(f"[ERROR] GTD ingest failed: {exc}")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Ingest & Aggregate GDELT V1
#
#  GDELT V1 export schema (tab-separated, NO header):
#    _c0  = GLOBALEVENTID (unique integer)
#    _c1  = SQLDATE       (YYYYMMDD integer)
#    _c7  = Actor1CountryCode  (3-letter ISO, e.g. "AFG")  ← join key country
#    _c53 = ActionGeo_CountryCode (FIPS, e.g. "AF")        ← event location
#
#  We derive year from _c1 (YYYYMMDD → first 4 chars) and use
#  Actor1CountryCode for the join. GDELT country codes are ISO-3166 alpha-3;
#  GTD country_txt is English name — so we broadcast a small lookup table
#  to translate GDELT codes → English names for the join key.
# ---------------------------------------------------------------------------

# Minimal ISO-3 → English name lookup for the most common countries in GTD.
# This avoids a full external lookup table dependency on EMR.
ISO3_TO_NAME = {
    "AFG": "Afghanistan", "IRQ": "Iraq", "PAK": "Pakistan", "SYR": "Syria",
    "IND": "India",       "NGA": "Nigeria", "COL": "Colombia", "PHL": "Philippines",
    "YEM": "Yemen",       "SOM": "Somalia", "LBY": "Libya",   "EGY": "Egypt",
    "TUR": "Turkey",      "RUS": "Russia",  "UKR": "Ukraine", "GBR": "United Kingdom",
    "FRA": "France",      "DEU": "Germany", "USA": "United States", "CHN": "China",
    "BRA": "Brazil",      "MEX": "Mexico",  "IDN": "Indonesia","BGD": "Bangladesh",
    "THA": "Thailand",    "KEN": "Kenya",   "ETH": "Ethiopia","TZA": "Tanzania",
    "MDN": "Sudan",       "SDN": "Sudan",   "CMR": "Cameroon","MLI": "Mali",
    "NER": "Niger",       "BFA": "Burkina Faso","LBN": "Lebanon","ISR": "Israel",
    "IRN": "Iran",        "SAU": "Saudi Arabia","JOR": "Jordan","MAR": "Morocco",
    "ALG": "Algeria",     "TUN": "Tunisia", "LKA": "Sri Lanka","NPL": "Nepal",
    "MMR": "Myanmar",     "KHM": "Cambodia","VNM": "Vietnam", "UZB": "Uzbekistan",
    "TJK": "Tajikistan",  "KGZ": "Kyrgyzstan","KAZ": "Kazakhstan","AZE": "Azerbaijan",
    "GEO": "Georgia",     "ARM": "Armenia", "MDA": "Moldova", "BLR": "Belarus",
    "SRB": "Serbia",      "HRV": "Croatia", "BIH": "Bosnia-Herzegovina",
    "ALB": "Albania",     "MKD": "North Macedonia","ESP": "Spain","ITA": "Italy",
    "GRC": "Greece",      "PRT": "Portugal","NLD": "Netherlands","BEL": "Belgium",
    "CHE": "Switzerland", "AUT": "Austria", "SWE": "Sweden",  "NOR": "Norway",
    "DNK": "Denmark",     "FIN": "Finland", "POL": "Poland",  "CZE": "Czech Republic",
    "HUN": "Hungary",     "ROU": "Romania", "BGR": "Bulgaria","SVK": "Slovakia",
    "ARG": "Argentina",   "CHL": "Chile",   "PER": "Peru",    "VEN": "Venezuela",
    "ECU": "Ecuador",     "BOL": "Bolivia", "PRY": "Paraguay","URY": "Uruguay",
    "GTM": "Guatemala",   "HND": "Honduras","SLV": "El Salvador","NIC": "Nicaragua",
    "CRI": "Costa Rica",  "PAN": "Panama",  "CUB": "Cuba",    "HTI": "Haiti",
    "DOM": "Dominican Republic","JAM": "Jamaica","TTO": "Trinidad and Tobago",
    "GHA": "Ghana",       "CIV": "Ivory Coast","SEN": "Senegal","GUI": "Guinea",
    "SLE": "Sierra Leone","LBR": "Liberia", "COD": "Democratic Republic of the Congo",
    "COG": "Republic of the Congo","UGA": "Uganda","RWA": "Rwanda","BDI": "Burundi",
    "MOZ": "Mozambique",  "ZWE": "Zimbabwe","ZMB": "Zambia",  "MWI": "Malawi",
    "AGO": "Angola",      "NAM": "Namibia", "BWA": "Botswana","ZAF": "South Africa",
    "AUS": "Australia",   "NZL": "New Zealand","CAN": "Canada","JPN": "Japan",
    "KOR": "South Korea", "PRK": "North Korea","MNG": "Mongolia","TWN": "Taiwan",
}

# Broadcast the lookup dict — it's tiny (~5 KB), avoids a shuffle join
iso3_broadcast = spark.sparkContext.broadcast(ISO3_TO_NAME)

@F.udf(StringType())
def iso3_to_country(code):
    """Translate GDELT ISO-3 actor country code to GTD-compatible English name."""
    if code is None:
        return None
    return iso3_broadcast.value.get(code.strip().upper(), None)

try:
    gdelt_raw = (
        spark.read
        .option("delimiter", "\t")
        .option("header", "false")
        .csv(GDELT_GLOBS)
    )

    # Extract year (first 4 chars of SQLDATE=_c1) and actor country (_c7)
    gdelt_filtered = (
        gdelt_raw
        .select(
            F.substring(F.col("_c1"), 1, 4).cast("int").alias("g_year"),
            F.col("_c7").alias("g_actor_iso3"),
        )
        .filter(F.col("g_year").isNotNull())
        .filter(F.col("g_actor_iso3").isNotNull())
        .filter(F.length(F.col("g_actor_iso3")) == 3)
    )

    # Translate ISO-3 code → English country name (same space as GTD)
    gdelt_named = gdelt_filtered.withColumn(
        "g_country", iso3_to_country(F.col("g_actor_iso3"))
    ).filter(F.col("g_country").isNotNull())

    # Aggregate: count GDELT events per (year, country) → tension score
    gdelt_agg = (
        gdelt_named
        .groupBy("g_year", "g_country")
        .agg(F.count("*").alias("gdelt_event_count"))
    )

    gdelt_count = gdelt_agg.cache().count()
    print(f"[GDELT] Aggregated into {gdelt_count:,} (year, country) buckets.")

except Exception as exc:
    print(f"[ERROR] GDELT ingest failed: {exc}")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 4. Distributed JOIN  (GTD left-join GDELT aggregation)
#    Broadcast GDELT aggregation — it's small after aggregation
# ---------------------------------------------------------------------------
print("[*] Performing LEFT JOIN (GTD ← GDELT) ...")
joined_df = (
    gtd_df
    .join(
        F.broadcast(gdelt_agg),
        (gtd_df.year    == gdelt_agg.g_year) &
        (gtd_df.country == gdelt_agg.g_country),
        how="left"
    )
    .drop("g_year", "g_country")
    .fillna({"gdelt_event_count": 0})
)

joined_count = joined_df.cache().count()
print(f"[JOIN] {joined_count:,} records in joined dataset.")

# ---------------------------------------------------------------------------
# 5. Llama-3 Instruct JSONL Formatting (UDF)
#    Output schema per record:
#      { "instruction": "...", "input": "", "output": "..." }
#    The fine_tuning notebook wraps these into the full chat template.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Horus, an elite Intelligence Analyst specializing in Open-Source "
    "Intelligence (OSINT). You synthesize tactical incident data (GTD) with "
    "macro-geopolitical event volumes (GDELT) to provide holistic threat assessments."
)

@F.udf(StringType())
def build_jsonl_record(year, country, attack_type, target_type, weapon_type,
                       kills, wounds, group_name, gdelt_count):
    """
    Constructs a single JSONL record in the Alpaca instruction format.
    This UDF runs distributed across all Spark executors.
    """
    if any(v is None for v in [year, country, attack_type]):
        return None

    # ── User instruction (the prompt the fine-tuned model will respond to)
    instruction = (
        f"Provide a brief intelligence summary of the {attack_type} incident "
        f"that occurred in {country} during {year}."
    )

    # ── Assistant response (the target output for supervised fine-tuning)
    kills_i  = int(kills)  if kills  else 0
    wounds_i = int(wounds) if wounds else 0
    gdelt_i  = int(gdelt_count) if gdelt_count else 0

    if kills_i > 0 or wounds_i > 0:
        casualty_str = (
            f"resulting in approximately {kills_i} reported "
            f"{'fatality' if kills_i == 1 else 'fatalities'} "
            f"and {wounds_i} wounded"
        )
    else:
        casualty_str = "with no casualties reported"

    target_str = f"The incident primarily targeted {target_type}." if target_type else ""
    weapon_str = f"The perpetrators utilized {weapon_type}." if weapon_type else ""

    group_str = ""
    if group_name and group_name.strip() not in ("", "Unknown"):
        group_str = f"Responsibility was attributed to {group_name}."

    if gdelt_i > 0:
        gdelt_str = (
            f"Concurrent GDELT analysis recorded {gdelt_i:,} geopolitical events "
            f"linked to {country} in {year}, indicating an elevated operational tempo."
        )
    else:
        gdelt_str = (
            f"No corroborating GDELT activity was indexed for {country} in {year}."
        )

    response_parts = [
        f"INTELLIGENCE REPORT — {attack_type.upper()} | {country} | {year}",
        "",
        f"According to GTD records, a {attack_type} incident took place in {country} "
        f"during {year}, {casualty_str}.",
        target_str,
        weapon_str,
        group_str,
        "",
        f"GEOPOLITICAL CONTEXT: {gdelt_str}",
        "",
        "THREAT ASSESSMENT: This incident is consistent with patterns of "
        f"{'high' if kills_i >= 10 else 'moderate' if kills_i >= 3 else 'low'}-severity "
        f"terrorism activity. Continued monitoring of {country} is recommended.",
    ]
    response = "\n".join(part for part in response_parts if part is not None)

    record = {
        "system":      SYSTEM_PROMPT,
        "instruction": instruction,
        "input":       "",
        "output":      response,
    }
    return json.dumps(record, ensure_ascii=False)


# Apply UDF to every row
print("[*] Applying Llama-3 formatting UDF ...")
formatted_df = (
    joined_df
    .withColumn(
        "jsonl",
        build_jsonl_record(
            F.col("year").cast("string"),
            F.col("country"),
            F.col("attack_type"),
            F.col("target_type"),
            F.col("weapon_type"),
            F.col("kills").cast("string"),
            F.col("wounds").cast("string"),
            F.col("group_name"),
            F.col("gdelt_event_count").cast("string"),
        )
    )
    .filter(F.col("jsonl").isNotNull())
    .select("jsonl")
)

total_formatted = formatted_df.cache().count()
print(f"[FORMAT] {total_formatted:,} valid JSONL records produced.")

# ---------------------------------------------------------------------------
# 6. Train / Val / Test Split  (80 / 10 / 10) — deterministic seed
# ---------------------------------------------------------------------------
print("[*] Splitting into train / val / test ...")
train_df, val_df, test_df = formatted_df.randomSplit([0.80, 0.10, 0.10], seed=42)

splits = {"train": train_df, "val": val_df, "test": test_df}
for split_name, split_df in splits.items():
    out_path = f"{OUTPUT_PATH}/{split_name}.jsonl"
    # coalesce(1) → single JSONL file per split (simplifies S3 download in Colab)
    # For very large datasets use coalesce(4) to avoid OOM on driver
    (
        split_df
        .coalesce(1)
        .write
        .mode("overwrite")
        .text(out_path)
    )
    cnt = split_df.count()
    print(f"[WRITE] {split_name:5s} → {out_path}  ({cnt:,} records)")

# ---------------------------------------------------------------------------
# 7. Verification — read back a sample from each split
# ---------------------------------------------------------------------------
print("\n[VERIFY] Sampling first record from each split ...")
for split_name in ["train", "val", "test"]:
    sample_path = f"{OUTPUT_PATH}/{split_name}.jsonl"
    try:
        sample = spark.read.text(sample_path).limit(1).collect()
        if sample:
            record = json.loads(sample[0]["value"])
            print(f"\n  [{split_name.upper()} SAMPLE]")
            print(f"  instruction : {record['instruction']}")
            print(f"  output (100c): {record['output'][:100]}...")
    except Exception as exc:
        print(f"  [WARN] Could not verify {split_name}: {exc}")

print("\n[DONE] Preprocessing complete. Terminating EMR cluster now.")
spark.stop()
