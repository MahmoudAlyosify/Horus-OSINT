# =============================================================================
#  CISC 886 – Cloud Computing | Queen's University
#  Horus-OSINT: Exploratory Data Analysis (EDA) on EMR / PySpark
#  Student NetID : 25bbdf-g23
#
#  PURPOSE:
#    Reads the processed JSONL splits from S3 and produces 3 EDA figures
#    required by Section 4 of the project rubric:
#      Figure 1 — Token Length Distribution (instruction + output combined)
#      Figure 2 — Attack Type (Label) Balance across the full dataset
#      Figure 3 — Sample Count per Split (train / val / test)
#
#    All figures are saved as PNG to /tmp on the EMR master node, then
#    uploaded to s3://horus-25bbdf-g23-bucket/eda/
#
#  EMR Step — Application location:
#    s3://horus-25bbdf-g23-bucket/scripts/eda_job.py
#
#  EMR Step — Arguments field:
#    (leave EMPTY — all config is hardcoded below)
#
#  NOTE: matplotlib and boto3 are pre-installed on EMR 6.x / 7.x AMIs.
#  No pip install needed.
# =============================================================================

import sys
import json
import os
import boto3
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType

# ---------------------------------------------------------------------------
# 0. Config — same bucket as the preprocessing job
# ---------------------------------------------------------------------------
BUCKET      = "horus-25bbdf-g23-bucket"
NETID       = "25bbdf-g23"
S3_BUCKET   = f"s3://{BUCKET}"
INPUT_PATH  = f"{S3_BUCKET}/processed"   # where train/val/test.jsonl live
EDA_S3_PATH = f"eda/"                    # S3 prefix for output PNGs
LOCAL_TMP   = "/tmp/eda"                 # temp dir on EMR master node

os.makedirs(LOCAL_TMP, exist_ok=True)

print("=" * 65)
print(f"[EDA] Bucket     : {S3_BUCKET}")
print(f"[EDA] Input      : {INPUT_PATH}")
print(f"[EDA] S3 output  : s3://{BUCKET}/{EDA_S3_PATH}")
print("=" * 65)

# ---------------------------------------------------------------------------
# 1. Spark Session
# ---------------------------------------------------------------------------
spark = (
    SparkSession.builder
    .appName(f"{NETID}-HorusOSINT-EDA")
    .config("spark.sql.shuffle.partitions",                  "50")
    .config("spark.sql.adaptive.enabled",                    "true")
    .config("spark.hadoop.fs.s3.impl",
            "com.amazon.ws.emr.hadoop.fs.EmrFileSystem")
    .config("spark.hadoop.fs.s3a.impl",
            "org.apache.hadoop.fs.s3a.S3AFileSystem")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("WARN")
print("[*] Spark Session started OK.")

# ---------------------------------------------------------------------------
# 2. Load all three splits and tag each row with its split name
# ---------------------------------------------------------------------------
# Each file is a folder (Spark output): processed/train.jsonl/part-00000
# spark.read.text() handles both the folder path and the part file correctly.

splits = {}
total_per_split = {}

for split_name in ["train", "val", "test"]:
    path = f"{INPUT_PATH}/{split_name}.jsonl"
    try:
        raw = spark.read.text(path)
        count = raw.count()
        splits[split_name] = raw
        total_per_split[split_name] = count
        print(f"[LOAD] {split_name:5s} : {count:,} records  ← {path}")
    except Exception as exc:
        print(f"[LOAD] WARNING — could not read {split_name}: {exc}")

if not splits:
    print("[EDA] FATAL — no split files could be read from S3.")
    spark.stop()
    sys.exit(1)

# ---------------------------------------------------------------------------
# 3. Parse JSONL and extract fields needed for EDA
# ---------------------------------------------------------------------------
# UDFs to extract fields from the raw JSON string in each row

@F.udf(IntegerType())
def token_length(json_str):
    """
    Approximate token count = whitespace-split word count of
    (instruction + output) concatenated.
    This matches the common heuristic used before actual tokenisation.
    """
    try:
        rec = json.loads(json_str)
        text = rec.get("instruction", "") + " " + rec.get("output", "")
        return len(text.split())
    except Exception:
        return None

@F.udf()
def extract_attack_type(json_str):
    """
    Extracts the attack type label from the 'output' field.
    The output always starts with:
      'INTELLIGENCE REPORT — <ATTACK_TYPE> | <COUNTRY> | <YEAR>'
    We parse the first line and grab the segment between '— ' and ' |'.
    """
    try:
        rec  = json.loads(json_str)
        line = rec.get("output", "").split("\n")[0]   # first line = header
        # Format: "INTELLIGENCE REPORT — BOMBING/EXPLOSION | Iraq | 2017"
        part = line.split("— ", 1)[1]                 # "BOMBING/EXPLOSION | Iraq | 2017"
        attack = part.split(" |")[0].strip().title()  # "Bombing/Explosion"
        return attack
    except Exception:
        return None

# Build a unified DataFrame with all splits tagged
tagged_dfs = []
for split_name, raw_df in splits.items():
    tagged = (
        raw_df
        .withColumn("split",       F.lit(split_name))
        .withColumn("token_len",   token_length(F.col("value")))
        .withColumn("attack_type", extract_attack_type(F.col("value")))
        .filter(F.col("token_len").isNotNull())
        .filter(F.col("attack_type").isNotNull())
    )
    tagged_dfs.append(tagged)

# Union all splits into one DataFrame
from functools import reduce
all_df = reduce(lambda a, b: a.union(b), tagged_dfs).cache()
total_all = all_df.count()
print(f"[EDA] Total records across all splits: {total_all:,}")

# ---------------------------------------------------------------------------
# 4. Collect aggregates to the driver  (small enough — no OOM risk)
# ---------------------------------------------------------------------------

# --- 4a. Token length histogram buckets (50-token bins up to 600) ---
# We bucket on the driver after collecting; Spark count gives us the raw values.
token_rows = (
    all_df
    .select("token_len")
    .filter(F.col("token_len") <= 800)   # cap outliers
    .collect()
)
token_lengths = [r["token_len"] for r in token_rows]
print(f"[EDA] Token length samples collected: {len(token_lengths):,}")
print(f"[EDA] Token length — min={min(token_lengths)}  "
      f"max={max(token_lengths)}  "
      f"mean={sum(token_lengths)//len(token_lengths)}")

# --- 4b. Attack type (label) distribution ---
attack_counts = (
    all_df
    .groupBy("attack_type")
    .agg(F.count("*").alias("count"))
    .orderBy(F.col("count").desc())
    .limit(12)            # top 12 attack types — covers >99% of GTD data
    .collect()
)
attack_labels  = [r["attack_type"] for r in attack_counts]
attack_values  = [r["count"]       for r in attack_counts]
print(f"[EDA] Attack types found: {len(attack_labels)}")

# --- 4c. Sample count per split ---
split_names  = list(total_per_split.keys())
split_counts = list(total_per_split.values())
print(f"[EDA] Split counts: {dict(zip(split_names, split_counts))}")

# ---------------------------------------------------------------------------
# 5. Generate Figures with matplotlib
# ---------------------------------------------------------------------------
import matplotlib
matplotlib.use("Agg")   # non-interactive backend — required on EMR (no display)
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np

HORUS_BLUE  = "#2C7BB6"
HORUS_RED   = "#D7191C"
HORUS_GRAY  = "#404040"
FIG_DPI     = 150

plt.rcParams.update({
    "font.family":      "DejaVu Sans",
    "axes.spines.top":  False,
    "axes.spines.right":False,
    "axes.grid":        True,
    "grid.alpha":       0.3,
    "axes.titlesize":   13,
    "axes.titleweight": "bold",
    "axes.labelsize":   11,
})

# ── Figure 1: Token Length Distribution ──────────────────────────────────────
fig1, ax1 = plt.subplots(figsize=(10, 4))

bins = range(0, 810, 25)   # 25-token bins
ax1.hist(token_lengths, bins=bins, color=HORUS_BLUE, edgecolor="white",
         linewidth=0.4, alpha=0.85)

mean_tok = int(sum(token_lengths) / len(token_lengths))
ax1.axvline(mean_tok, color=HORUS_RED, linewidth=1.8, linestyle="--",
            label=f"Mean = {mean_tok} tokens")

ax1.set_title("Figure 1 — Token Length Distribution\n"
              "(instruction + output word count, all splits combined)")
ax1.set_xlabel("Approximate Token Count (words)")
ax1.set_ylabel("Number of Training Samples")
ax1.legend(frameon=False)
ax1.xaxis.set_major_locator(ticker.MultipleLocator(50))

fig1.tight_layout()
fig1_path = f"{LOCAL_TMP}/fig1_token_length_distribution.png"
fig1.savefig(fig1_path, dpi=FIG_DPI)
plt.close(fig1)
print(f"[FIG1] Saved: {fig1_path}")

# ── Figure 2: Attack Type (Label) Balance ────────────────────────────────────
fig2, ax2 = plt.subplots(figsize=(11, 5))

# Horizontal bar chart — easier to read long category names
y_pos = np.arange(len(attack_labels))
bars  = ax2.barh(y_pos, attack_values, color=HORUS_BLUE, edgecolor="white",
                 linewidth=0.4, alpha=0.85)

# Annotate bars with exact counts
for bar, val in zip(bars, attack_values):
    ax2.text(bar.get_width() + max(attack_values) * 0.005,
             bar.get_y() + bar.get_height() / 2,
             f"{val:,}", va="center", ha="left", fontsize=9, color=HORUS_GRAY)

ax2.set_yticks(y_pos)
ax2.set_yticklabels(attack_labels, fontsize=10)
ax2.invert_yaxis()   # most frequent at top
ax2.set_title("Figure 2 — Attack Type Label Balance\n"
              "(top attack categories across all splits — GTD dataset)")
ax2.set_xlabel("Number of Samples")
ax2.set_xlim(0, max(attack_values) * 1.15)

fig2.tight_layout()
fig2_path = f"{LOCAL_TMP}/fig2_attack_type_balance.png"
fig2.savefig(fig2_path, dpi=FIG_DPI)
plt.close(fig2)
print(f"[FIG2] Saved: {fig2_path}")

# ── Figure 3: Sample Count per Split ─────────────────────────────────────────
fig3, ax3 = plt.subplots(figsize=(6, 4))

colors = [HORUS_BLUE, "#74ADD1", "#ABD9E9"]
bar3   = ax3.bar(split_names, split_counts, color=colors,
                 edgecolor="white", linewidth=0.6, width=0.5)

# Annotate with counts and percentages
total = sum(split_counts)
for bar, val in zip(bar3, split_counts):
    pct = 100 * val / total
    ax3.text(bar.get_x() + bar.get_width() / 2,
             bar.get_height() + total * 0.005,
             f"{val:,}\n({pct:.0f}%)",
             ha="center", va="bottom", fontsize=10, color=HORUS_GRAY)

ax3.set_title("Figure 3 — Sample Count per Split\n"
              "(80 / 10 / 10 train-val-test partition)")
ax3.set_xlabel("Dataset Split")
ax3.set_ylabel("Number of JSONL Records")
ax3.set_ylim(0, max(split_counts) * 1.18)
ax3.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{int(x):,}"))

fig3.tight_layout()
fig3_path = f"{LOCAL_TMP}/fig3_sample_count_per_split.png"
fig3.savefig(fig3_path, dpi=FIG_DPI)
plt.close(fig3)
print(f"[FIG3] Saved: {fig3_path}")

# ---------------------------------------------------------------------------
# 6. Upload all 3 PNGs to S3
# ---------------------------------------------------------------------------
s3_client = boto3.client("s3")

figures = [
    (fig1_path, "fig1_token_length_distribution.png"),
    (fig2_path, "fig2_attack_type_balance.png"),
    (fig3_path, "fig3_sample_count_per_split.png"),
]

print("\n[UPLOAD] Uploading figures to S3 ...")
for local_path, filename in figures:
    s3_key = f"{EDA_S3_PATH}{filename}"
    try:
        s3_client.upload_file(local_path, BUCKET, s3_key)
        print(f"[UPLOAD] ✓  s3://{BUCKET}/{s3_key}")
    except Exception as exc:
        print(f"[UPLOAD] ✗  Failed to upload {filename}: {exc}")

# ---------------------------------------------------------------------------
# 7. Print EDA Summary to stdout (appears in EMR Step logs)
# ---------------------------------------------------------------------------
print("\n" + "=" * 65)
print("[EDA SUMMARY]")
print(f"  Total records (all splits) : {total_all:,}")
print(f"  Train / Val / Test         : "
      f"{total_per_split.get('train',0):,} / "
      f"{total_per_split.get('val',0):,} / "
      f"{total_per_split.get('test',0):,}")
print(f"  Token length — mean        : {mean_tok} words")
print(f"  Token length — min/max     : {min(token_lengths)} / {max(token_lengths)}")
print(f"  Attack types (unique)      : {len(attack_labels)}")
print(f"  Top attack type            : {attack_labels[0]} ({attack_values[0]:,} samples)")
print("\n  EDA figures saved to:")
for _, fname in figures:
    print(f"    s3://{BUCKET}/{EDA_S3_PATH}{fname}")
print("=" * 65)
print("[DONE] EDA complete. *** TERMINATE THE EMR CLUSTER NOW ***")

spark.stop()
