# =============================================================================
#  CISC 886 – Cloud Computing | Queen's University
#  Horus-OSINT: Data Preprocessing Pipeline (AWS EMR / PySpark)
#  Student NetID : 25bbdf-g23
#
#  FIXES applied for Queen's Sandbox EMR (vs original):
#    1. Removed multiLine=True  → was causing S3 read hangs on large CSVs
#    2. Fixed GDELT glob pattern → removed ".export" suffix (wrong filename)
#    3. Removed post-write count() → was recomputing full DAG, wasting cost
#    4. Renamed UDF param gdelt_count → gdelt_evt_count → avoids column clash
#    5. Added spark.hadoop.fs.s3.impl config → explicit EMR 7.x S3 connector
#
#  EMR Step Arguments field:
#    --bucket horus-25bbdf-g23-bucket
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
# 0. Argument Parsing
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Horus-OSINT PySpark Preprocessing")
parser.add_argument("--bucket",      default="horus-25bbdf-g23-bucket")
parser.add_argument("--gtd_key",     default="raw/gtd/gtd_merged.csv")
parser.add_argument("--gdelt_years", default="2015,2016,2017,2018,2019")
parser.add_argument("--max_records", type=int, default=200_000)
args, _ = parser.parse_known_args()

NETID        = "25bbdf-g23"
S3_BUCKET    = f"s3://{args.bucket}"
GTD_RAW_PATH = f"{S3_BUCKET}/{args.gtd_key}"
OUTPUT_PATH  = f"{S3_BUCKET}/processed"
GDELT_YEARS  = [y.strip() for y in args.gdelt_years.split(",")]

# FIX #2: Correct GDELT V1 public bucket filename pattern.
# Real filenames: s3://gdelt-open-data/events/20150101.CSV  (NO ".export" suffix)
GDELT_GLOBS = [
    f"s3://gdelt-open-data/events/{year}*.CSV"
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
    .config("spark.sql.shuffle.partitions", "100")
    .config("spark.sql.adaptive.enabled", "true")
    .config("spark.sql.adaptive.coalescePartitions.enabled", "true")
    # FIX #5: Explicit S3 connector for EMR 7.x — avoids s3a:// vs s3:// confusion
    .config("spark.hadoop.fs.s3.impl",
            "com.amazon.ws.emr.hadoop.fs.EmrFileSystem")
    .config("spark.hadoop.fs.s3a.impl",
            "org.apache.hadoop.fs.s3a.S3AFileSystem")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")
print("[*] Spark Session started.")

# ---------------------------------------------------------------------------
# 2. Ingest GTD Data
# ---------------------------------------------------------------------------
GTD_COLUMNS = {
    "iyear":           "year",
    "country_txt":     "country",
    "attacktype1_txt": "attack_type",
    "targtype1_txt":   "target_type",
    "weaptype1_txt":   "weapon_type",
    "nkill":           "kills",
    "nwound":          "wounds",
    "gname":           "group_name",
    "summary":         "summary",
}

try:
    gtd_raw = (
        spark.read
        .option("header", "true")
        .option("inferSchema", "false")
        # FIX #1: multiLine=True removed — causes S3 read hangs on >100MB CSVs.
        # GTD summaries may contain newlines but Spark handles most cases without it.
        # .option("multiLine", "true")   ← REMOVED
        .option("escape", '"')
        .option("quote", '"')
        .option("mode", "PERMISSIVE")   # don't crash on malformed rows
        .csv(GTD_RAW_PATH)
    )

    select_exprs = [
        F.col(src).alias(dst)
        for src, dst in GTD_COLUMNS.items()
        if src in gtd_raw.columns
    ]
    gtd_df = gtd_raw.select(select_exprs)

    gtd_df = (
        gtd_df
        .withColumn("year",   F.col("year").cast("int"))
        .withColumn("kills",  F.coalesce(F.col("kills").cast("double"),  F.lit(0.0)))
        .withColumn("wounds", F.coalesce(F.col("wounds").cast("double"), F.lit(0.0)))
        .dropna(subset=["year", "country", "attack_type"])
        .filter(F.col("attack_type") != "Unknown")
    )

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
# ---------------------------------------------------------------------------

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

iso3_broadcast = spark.sparkContext.broadcast(ISO3_TO_NAME)

@F.udf(StringType())
def iso3_to_country(code):
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

    gdelt_named = gdelt_filtered.withColumn(
        "g_country", iso3_to_country(F.col("g_actor_iso3"))
    ).filter(F.col("g_country").isNotNull())

    gdelt_agg = (
        gdelt_named
        .groupBy("g_year", "g_country")
        # FIX #4: renamed alias to gdelt_evt_count to avoid UDF parameter clash
        .agg(F.count("*").alias("gdelt_evt_count"))
    )

    gdelt_count = gdelt_agg.cache().count()
    print(f"[GDELT] Aggregated into {gdelt_count:,} (year, country) buckets.")

except Exception as exc:
    print(f"[ERROR] GDELT ingest failed: {exc}")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 4. Distributed JOIN (GTD left-join GDELT aggregation)
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
    # FIX #4: fillna key matches the renamed alias
    .fillna({"gdelt_evt_count": 0})
)

joined_count = joined_df.cache().count()
print(f"[JOIN] {joined_count:,} records in joined dataset.")

# ---------------------------------------------------------------------------
# 5. Llama-3 Instruct JSONL Formatting (UDF)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Horus, an elite Intelligence Analyst specializing in Open-Source "
    "Intelligence (OSINT). You synthesize tactical incident data (GTD) with "
    "macro-geopolitical event volumes (GDELT) to provide holistic threat assessments."
)

@F.udf(StringType())
def build_jsonl_record(year, country, attack_type, target_type, weapon_type,
                       kills, wounds, group_name, gdelt_evt_count):
    """
    Constructs a single JSONL record in the Alpaca instruction format.
    Runs distributed across all Spark executors.
    """
    if any(v is None for v in [year, country, attack_type]):
        return None

    instruction = (
        f"Provide a brief intelligence summary of the {attack_type} incident "
        f"that occurred in {country} during {year}."
    )

    kills_i  = int(float(kills))         if kills         else 0
    wounds_i = int(float(wounds))        if wounds        else 0
    # FIX #4: param name matches the new alias
    gdelt_i  = int(float(gdelt_evt_count)) if gdelt_evt_count else 0

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
            # FIX #4: column name matches renamed alias
            F.col("gdelt_evt_count").cast("string"),
        )
    )
    .filter(F.col("jsonl").isNotNull())
    .select("jsonl")
)

total_formatted = formatted_df.cache().count()
print(f"[FORMAT] {total_formatted:,} valid JSONL records produced.")

# ---------------------------------------------------------------------------
# 6. Train / Val / Test Split (80 / 10 / 10)
# ---------------------------------------------------------------------------
print("[*] Splitting into train / val / test ...")
train_df, val_df, test_df = formatted_df.randomSplit([0.80, 0.10, 0.10], seed=42)

splits = {"train": train_df, "val": val_df, "test": test_df}
for split_name, split_df in splits.items():
    out_path = f"{OUTPUT_PATH}/{split_name}.jsonl"
    (
        split_df
        .coalesce(1)
        .write
        .mode("overwrite")
        .text(out_path)
    )
    # FIX #3: removed post-write count() — avoids recomputing the full DAG.
    # Split counts are approximate (80/10/10 of total_formatted).
    print(f"[WRITE] {split_name:5s} → {out_path}")

print(f"  Approximate sizes: train~{int(total_formatted*0.8):,}  "
      f"val~{int(total_formatted*0.1):,}  test~{int(total_formatted*0.1):,}")

# ---------------------------------------------------------------------------
# 7. Verification — read back 1 record from each split
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
            print(f"  output(100c): {record['output'][:100]}...")
    except Exception as exc:
        print(f"  [WARN] Could not verify {split_name}: {exc}")

print("\n[DONE] Preprocessing complete. TERMINATE THE EMR CLUSTER NOW.")
spark.stop()