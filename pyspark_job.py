# =============================================================================
#  CISC 886 – Cloud Computing | Queen's University
#  Horus-OSINT: Data Preprocessing Pipeline (AWS EMR / PySpark)
#  Student NetID : 25bbdf-g23
#
#  CONFIRMED CONFIG:
#    Bucket  : horus-25bbdf-g23-bucket
#    GTD file: raw/gtd/GTD_Merged_Full.csv  (196 MB, plain CSV)
#    GDELT   : MANDATORY (s3://gdelt-open-data/events/)
#
#  FIX v3:
#    - Added .option("compression", "none") on GTD read → fixes ZipException
#    - Added .option("compression", "none") on GDELT read → same protection
#    - Added .option("maxColumns", "200")   → GTD has many columns, avoid parse errors
#    - Added .option("charToEscapeQuoteEscaping", "\\") → handles GTD quote edge cases
#
#  EMR Step — Application location:
#    s3://horus-25bbdf-g23-bucket/scripts/pyspark_job.py
#
#  EMR Step — Arguments field:
#    (leave EMPTY)
# =============================================================================

import sys
import json
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import StringType

# ---------------------------------------------------------------------------
# 0. Hardcoded Config
# ---------------------------------------------------------------------------
BUCKET      = "horus-25bbdf-g23-bucket"
GTD_KEY     = "raw/gtd/GTD_Merged_Full.csv"
GDELT_YEARS = ["2015", "2016", "2017", "2018", "2019"]
MAX_RECORDS = 200_000
NETID       = "25bbdf-g23"

S3_BUCKET    = f"s3://{BUCKET}"
GTD_RAW_PATH = f"{S3_BUCKET}/{GTD_KEY}"
OUTPUT_PATH  = f"{S3_BUCKET}/processed"
GDELT_GLOBS  = [f"s3://gdelt-open-data/events/{y}*.csv" for y in GDELT_YEARS]

print("=" * 65)
print(f"[CONFIG] Bucket    : {S3_BUCKET}")
print(f"[CONFIG] GTD source: {GTD_RAW_PATH}")
print(f"[CONFIG] GDELT     : {len(GDELT_YEARS)} years ({GDELT_YEARS[0]}-{GDELT_YEARS[-1]})")
print(f"[CONFIG] Output    : {OUTPUT_PATH}")
print("=" * 65)

# ---------------------------------------------------------------------------
# 1. Spark Session
# ---------------------------------------------------------------------------
spark = (
    SparkSession.builder
    .appName(f"{NETID}-HorusOSINT-DataPrep")
    .config("spark.sql.shuffle.partitions",                     "100")
    .config("spark.sql.adaptive.enabled",                       "true")
    .config("spark.sql.adaptive.coalescePartitions.enabled",    "true")
    .config("spark.hadoop.fs.s3.impl",
            "com.amazon.ws.emr.hadoop.fs.EmrFileSystem")
    .config("spark.hadoop.fs.s3a.impl",
            "org.apache.hadoop.fs.s3a.S3AFileSystem")
    .config("spark.network.timeout",            "800s")
    .config("spark.executor.heartbeatInterval", "60s")
    .config("spark.sql.broadcastTimeout",       "600")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")
print("[*] Spark Session started OK.")

# ---------------------------------------------------------------------------
# 2. Ingest GTD  (GTD_Merged_Full.csv — 196 MB plain CSV)
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

print(f"[GTD] Reading: {GTD_RAW_PATH}")
try:
    gtd_raw = (
        spark.read
        .option("header",                    "true")
        .option("inferSchema",               "false")
        # ── FIX v3: tell Spark this is a plain uncompressed CSV ──
        .option("compression",               "none")
        # ── handle GTD's messy quoting ──
        .option("escape",                    '"')
        .option("quote",                     '"')
        .option("charToEscapeQuoteEscaping", "\\")
        .option("mode",                      "PERMISSIVE")
        # ── GTD has ~135 columns — raise the default limit ──
        .option("maxColumns",                "200")
        .csv(GTD_RAW_PATH)
    )

    actual_cols = gtd_raw.columns
    print(f"[GTD] Total columns in CSV : {len(actual_cols)}")
    print(f"[GTD] First 15 column names: {actual_cols[:15]}")

    missing = [src for src in GTD_COLUMNS if src not in actual_cols]
    if missing:
        print(f"[GTD] WARNING — expected columns not found: {missing}")

    select_exprs = [
        F.col(src).alias(dst)
        for src, dst in GTD_COLUMNS.items()
        if src in actual_cols
    ]

    if not select_exprs:
        print("[GTD] FATAL — none of the expected GTD columns exist!")
        print(f"[GTD] Expected : {list(GTD_COLUMNS.keys())}")
        print(f"[GTD] Found    : {actual_cols[:20]}")
        spark.stop()
        sys.exit(1)

    gtd_df = (
        gtd_raw.select(select_exprs)
        .withColumn("year",   F.col("year").cast("int"))
        .withColumn("kills",  F.coalesce(F.col("kills").cast("double"),  F.lit(0.0)))
        .withColumn("wounds", F.coalesce(F.col("wounds").cast("double"), F.lit(0.0)))
        .dropna(subset=["year", "country", "attack_type"])
        .filter(F.col("attack_type") != "Unknown")
    )

    if MAX_RECORDS > 0:
        gtd_df = gtd_df.limit(MAX_RECORDS)

    gtd_count = gtd_df.cache().count()
    print(f"[GTD] Loaded {gtd_count:,} clean records.")

    if gtd_count == 0:
        print("[GTD] FATAL — 0 records loaded. Check column names above.")
        spark.stop()
        sys.exit(1)

except Exception as exc:
    print(f"[GTD] FATAL — {exc}")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Ingest & Aggregate GDELT V1  (MANDATORY)
# ---------------------------------------------------------------------------
# GDELT V1 column layout (no header):
#   _c1 = SQLDATE (YYYYMMDD) → year = first 4 chars
#   _c7 = Actor1CountryCode  → ISO3

ISO3_TO_NAME = {
    "AFG": "Afghanistan",    "IRQ": "Iraq",           "PAK": "Pakistan",
    "SYR": "Syria",          "IND": "India",          "NGA": "Nigeria",
    "COL": "Colombia",       "PHL": "Philippines",    "YEM": "Yemen",
    "SOM": "Somalia",        "LBY": "Libya",          "EGY": "Egypt",
    "TUR": "Turkey",         "RUS": "Russia",         "UKR": "Ukraine",
    "GBR": "United Kingdom", "FRA": "France",         "DEU": "Germany",
    "USA": "United States",  "CHN": "China",          "BRA": "Brazil",
    "MEX": "Mexico",         "IDN": "Indonesia",      "BGD": "Bangladesh",
    "THA": "Thailand",       "KEN": "Kenya",          "ETH": "Ethiopia",
    "SDN": "Sudan",          "CMR": "Cameroon",       "MLI": "Mali",
    "NER": "Niger",          "BFA": "Burkina Faso",   "LBN": "Lebanon",
    "ISR": "Israel",         "IRN": "Iran",           "SAU": "Saudi Arabia",
    "JOR": "Jordan",         "MAR": "Morocco",        "DZA": "Algeria",
    "TUN": "Tunisia",        "LKA": "Sri Lanka",      "NPL": "Nepal",
    "MMR": "Myanmar",        "VNM": "Vietnam",        "UZB": "Uzbekistan",
    "KAZ": "Kazakhstan",     "AZE": "Azerbaijan",     "GEO": "Georgia",
    "SRB": "Serbia",         "HRV": "Croatia",        "BIH": "Bosnia-Herzegovina",
    "ESP": "Spain",          "ITA": "Italy",          "GRC": "Greece",
    "POL": "Poland",         "NLD": "Netherlands",    "BEL": "Belgium",
    "SWE": "Sweden",         "NOR": "Norway",         "DNK": "Denmark",
    "ARG": "Argentina",      "CHL": "Chile",          "PER": "Peru",
    "VEN": "Venezuela",      "GTM": "Guatemala",      "CUB": "Cuba",
    "HTI": "Haiti",          "DOM": "Dominican Republic",
    "GHA": "Ghana",          "CIV": "Ivory Coast",    "SEN": "Senegal",
    "COD": "Democratic Republic of the Congo",
    "UGA": "Uganda",         "RWA": "Rwanda",         "MOZ": "Mozambique",
    "ZWE": "Zimbabwe",       "ZAF": "South Africa",   "AGO": "Angola",
    "AUS": "Australia",      "CAN": "Canada",         "JPN": "Japan",
    "KOR": "South Korea",    "TWN": "Taiwan",
}

iso3_broadcast = spark.sparkContext.broadcast(ISO3_TO_NAME)

@F.udf(StringType())
def iso3_to_country(code):
    if code is None:
        return None
    return iso3_broadcast.value.get(code.strip().upper(), None)

print(f"[GDELT] Reading years {GDELT_YEARS} from s3://gdelt-open-data/events/ ...")

try:
    gdelt_raw = (
        spark.read
        .option("delimiter",   "\t")
        .option("header",      "false")
        # ── FIX v3: explicit no-compression on GDELT too ──
        .option("compression", "none")
        .option("maxColumns",  "60")
        .csv(GDELT_GLOBS)
    )

    num_cols = len(gdelt_raw.columns)
    print(f"[GDELT] Columns detected: {num_cols} (expected 57 for GDELT V1)")

    if num_cols < 8:
        raise ValueError(f"GDELT has only {num_cols} columns — expected 57.")

    gdelt_filtered = (
        gdelt_raw
        .select(
            F.substring(F.col("_c1"), 1, 4).cast("int").alias("g_year"),
            F.col("_c7").alias("g_actor_iso3"),
        )
        .filter(F.col("g_year").isNotNull())
        .filter(F.col("g_actor_iso3").isNotNull())
        .filter(F.length(F.col("g_actor_iso3")) == 3)
        .filter(F.col("g_year").isin([int(y) for y in GDELT_YEARS]))
    )

    gdelt_named = (
        gdelt_filtered
        .withColumn("g_country", iso3_to_country(F.col("g_actor_iso3")))
        .filter(F.col("g_country").isNotNull())
    )

    gdelt_agg = (
        gdelt_named
        .groupBy("g_year", "g_country")
        .agg(F.count("*").alias("gdelt_evt_count"))
    )

    gdelt_count = gdelt_agg.cache().count()
    print(f"[GDELT] Aggregated: {gdelt_count:,} (year, country) buckets.")

    if gdelt_count == 0:
        raise ValueError("GDELT aggregation returned 0 rows.")

except Exception as exc:
    print(f"[GDELT] FATAL — {exc}")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 4. LEFT JOIN  GTD ← GDELT
# ---------------------------------------------------------------------------
print("[*] LEFT JOIN GTD ← GDELT ...")

joined_df = (
    gtd_df
    .join(
        F.broadcast(gdelt_agg),
        (gtd_df.year    == gdelt_agg.g_year) &
        (gtd_df.country == gdelt_agg.g_country),
        how="left"
    )
    .drop("g_year", "g_country")
    .fillna({"gdelt_evt_count": 0})
)

joined_count = joined_df.cache().count()
matched      = joined_df.filter(F.col("gdelt_evt_count") > 0).count()
print(f"[JOIN] Total  : {joined_count:,}")
print(f"[JOIN] Matched: {matched:,} ({100 * matched // max(joined_count, 1)}%)")

if joined_count == 0:
    print("[JOIN] FATAL — empty join result.")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 5. Llama-3 Instruct JSONL Formatting
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are Horus, an elite Intelligence Analyst specializing in Open-Source "
    "Intelligence (OSINT). You synthesize tactical incident data (GTD) with "
    "macro-geopolitical event volumes (GDELT) to provide holistic threat assessments."
)

@F.udf(StringType())
def build_jsonl_record(year, country, attack_type, target_type, weapon_type,
                       kills, wounds, group_name, gdelt_evt_count):
    if any(v is None for v in [year, country, attack_type]):
        return None

    kills_i  = int(float(kills))           if kills           else 0
    wounds_i = int(float(wounds))          if wounds          else 0
    gdelt_i  = int(float(gdelt_evt_count)) if gdelt_evt_count else 0

    instruction = (
        f"Provide a brief intelligence summary of the {attack_type} incident "
        f"that occurred in {country} during {year}."
    )
    casualty_str = (
        f"resulting in approximately {kills_i} reported "
        f"{'fatality' if kills_i == 1 else 'fatalities'} and {wounds_i} wounded"
        if (kills_i > 0 or wounds_i > 0) else
        "with no casualties reported"
    )
    target_str = f"The incident primarily targeted {target_type}." if target_type else ""
    weapon_str = f"The perpetrators utilized {weapon_type}."       if weapon_type else ""
    group_str  = (
        f"Responsibility was attributed to {group_name}."
        if group_name and group_name.strip() not in ("", "Unknown") else ""
    )
    gdelt_str = (
        f"Concurrent GDELT analysis recorded {gdelt_i:,} geopolitical events "
        f"linked to {country} in {year}, indicating an elevated operational tempo."
        if gdelt_i > 0 else
        f"No corroborating GDELT activity was indexed for {country} in {year}."
    )
    severity = "high" if kills_i >= 10 else "moderate" if kills_i >= 3 else "low"

    response = "\n".join(p for p in [
        f"INTELLIGENCE REPORT — {attack_type.upper()} | {country} | {year}",
        "",
        f"According to GTD records, a {attack_type} incident took place in "
        f"{country} during {year}, {casualty_str}.",
        target_str, weapon_str, group_str,
        "",
        f"GEOPOLITICAL CONTEXT: {gdelt_str}",
        "",
        f"THREAT ASSESSMENT: This incident is consistent with patterns of "
        f"{severity}-severity terrorism activity. "
        f"Continued monitoring of {country} is recommended.",
    ] if p is not None)

    return json.dumps({
        "system":      SYSTEM_PROMPT,
        "instruction": instruction,
        "input":       "",
        "output":      response,
    }, ensure_ascii=False)


print("[*] Applying Llama-3 JSONL formatting UDF ...")
formatted_df = (
    joined_df
    .withColumn("jsonl", build_jsonl_record(
        F.col("year").cast("string"),
        F.col("country"),
        F.col("attack_type"),
        F.col("target_type"),
        F.col("weapon_type"),
        F.col("kills").cast("string"),
        F.col("wounds").cast("string"),
        F.col("group_name"),
        F.col("gdelt_evt_count").cast("string"),
    ))
    .filter(F.col("jsonl").isNotNull())
    .select("jsonl")
)

total_formatted = formatted_df.cache().count()
print(f"[FORMAT] {total_formatted:,} valid JSONL records produced.")

if total_formatted == 0:
    print("[FORMAT] FATAL — 0 records. UDF filtered everything out.")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 6. Train / Val / Test Split  80 / 10 / 10
# ---------------------------------------------------------------------------
print("[*] Splitting 80 / 10 / 10 ...")
train_df, val_df, test_df = formatted_df.randomSplit([0.80, 0.10, 0.10], seed=42)

for split_name, split_df in [("train", train_df), ("val", val_df), ("test", test_df)]:
    out_path = f"{OUTPUT_PATH}/{split_name}.jsonl"
    split_df.coalesce(1).write.mode("overwrite").text(out_path)
    print(f"[WRITE] {split_name:5s} → {out_path}/part-00000")

print(f"\n  Approximate sizes:")
print(f"    train ~{int(total_formatted * 0.8):,}  |  "
      f"val ~{int(total_formatted * 0.1):,}  |  "
      f"test ~{int(total_formatted * 0.1):,}")

# ---------------------------------------------------------------------------
# 7. Verify — read back 1 record from each split
# ---------------------------------------------------------------------------
print("\n[VERIFY] Reading back one sample per split ...")
for split_name in ["train", "val", "test"]:
    try:
        sample = spark.read.text(f"{OUTPUT_PATH}/{split_name}.jsonl").limit(1).collect()
        if sample:
            rec = json.loads(sample[0]["value"])
            print(f"\n  [{split_name.upper()}] {rec['instruction']}")
            print(f"           {rec['output'][:120]}...")
        else:
            print(f"  [WARN] {split_name} is empty!")
    except Exception as exc:
        print(f"  [WARN] {split_name} verify failed: {exc}")

print("\n" + "=" * 65)
print("[DONE] Pipeline complete!")
print(f"[DONE] s3://{BUCKET}/processed/train.jsonl/part-00000")
print(f"[DONE] s3://{BUCKET}/processed/val.jsonl/part-00000")
print(f"[DONE] s3://{BUCKET}/processed/test.jsonl/part-00000")
print("[DONE] *** TERMINATE THE EMR CLUSTER NOW ***")
print("=" * 65)
spark.stop()
