# CISC 886: Horus-OSINT — Cloud-Based Threat Intelligence Assistant

**Queen's University — School of Computing**  
**Course:** CISC 886 — Cloud Computing  
**Student NetID:** 25BBDF-G23  

---

## Overview

**Horus-OSINT** is a production-grade, cloud-native conversational intelligence assistant trained to function as an elite Open-Source Intelligence (OSINT) and Global Threat Analyst. The system fine-tunes `Meta-Llama-3-8B-Instruct` on 159,826 records derived from the **Global Terrorism Database (GTD)** and **GDELT** datasets, then deploys the resulting model as a fully interactive web application on AWS.

The entire pipeline — from raw data ingestion through distributed preprocessing, GPU fine-tuning, GGUF quantization, and live web deployment — runs on managed AWS services within a strict $90 budget constraint.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA INGESTION                              │
│  S3: horus-25bbdf-g23-bucket/raw/                                   │
│  ├── gtd/gtd_merged.csv          (GTD — uploaded manually)          │
│  └── [GDELT]                     (AWS Public Dataset — direct read)  │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DISTRIBUTED PREPROCESSING                        │
│  AWS EMR Cluster: 25bbdf-g23-emr  [TERMINATED]                     │
│  ├── 1× Master  — m5.xlarge                                         │
│  └── 2× Core    — m5.xlarge                                         │
│  PySpark Job: pyspark_job.py                                        │
│  Output: 159,826 Llama-3 formatted JSONL records                    │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│               S3: horus-25bbdf-g23-bucket/processed/                │
│               ├── train.jsonl   (159,826 samples)                   │
│               ├── val.jsonl                                         │
│               └── test.jsonl                                        │
└──────────────┬───────────────────────────────────────────────────── ┘
               │
               │                  ┌────────────────────────────────┐
               └─────────────────►│   Google Colab — T4 GPU (16GB) │
                                  │   RUN_horus_osint_final_v3.ipynb│
                                  │                                 │
                                  │  • Unsloth QLoRA (4-bit NF4)   │
                                  │  • LoRA r=16, α=32             │
                                  │  • 500 steps, lr=2e-4          │
                                  │  • Export: q4_k_m GGUF (4.92GB)│
                                  └──────────────┬─────────────────┘
                                                 │
                                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│         S3: horus-25bbdf-g23-bucket/models/                         │
│         └── horus-llama3-osint-Q4_K_M.gguf   (4.92 GB)             │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│              AWS EC2: 25bbdf-g23-ec2  (g4dn.xlarge)                 │
│              1× NVIDIA T4 GPU — 16GB VRAM                           │
│              ├── Ollama   → port 11434 (VPC-internal only)          │
│              └── OpenWebUI (Docker) → port 8080 (public)            │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                            ▼
              User Browser → http://<ec2-public-ip>:8080
```

---

## Repository Structure

```
.
├── main.tf                              # Terraform IaC — VPC, Subnet, IGW, SG, S3
├── pyspark_job.py                       # PySpark preprocessing pipeline (EMR)
├── RUN_horus_osint_final_v3.ipynb       # ✅ Executed fine-tuning notebook (Colab T4)
└── README.md                            # This file
```

---

## Prerequisites

| Requirement | Version / Notes |
|---|---|
| AWS Account | Region: `us-east-1` |
| Terraform | `>= 1.3` |
| Python | `3.10+` |
| Apache Spark | `3.5` (via EMR 7.0.0) |
| Google Colab | T4 GPU runtime |
| HuggingFace Token | With gated access to `meta-llama/Meta-Llama-3-8B-Instruct` |
| AWS CLI | Configured with project IAM credentials |

---

## Step-by-Step Replication Guide

### Step 1 — Infrastructure Provisioning

```bash
# Clone the repository
git clone https://github.com/MahmoudAlyosify/Horus-OSINT
cd Horus-OSINT

# Initialise Terraform providers
terraform init

# Preview the planned resources
terraform plan

# Apply — creates VPC, Subnet, IGW, Route Table, Security Group, S3 Bucket
terraform apply -auto-approve
```

**Outputs to record after apply:**

| Output Key | Example Value |
|---|---|
| `vpc_id` | `vpc-0abc...` |
| `subnet_id` | `subnet-0xyz...` |
| `security_group_id` | `sg-0def...` |
| `s3_bucket_name` | `horus-25bbdf-g23-bucket` |

---

### Step 2 — Upload Raw Data to S3

```bash
# Upload the merged GTD dataset to S3
aws s3 cp gtd_merged.csv s3://horus-25bbdf-g23-bucket/raw/gtd/gtd_merged.csv

# GDELT is read directly from the AWS Open Data Registry — no upload required:
# s3://gdelt-open-data/events/  (accessed inside the PySpark script)
```

---

### Step 3 — Distributed Preprocessing on AWS EMR

#### 3a. Upload the PySpark script

```bash
aws s3 cp pyspark_job.py s3://horus-25bbdf-g23-bucket/scripts/pyspark_job.py
```

#### 3b. Launch EMR Cluster via AWS Console

| Setting | Value |
|---|---|
| Cluster Name | `25bbdf-g23-emr` |
| EMR Release | `emr-7.0.0` (Spark 3.5) |
| Master Node | `1 × m5.xlarge` |
| Core Nodes | `2 × m5.xlarge` |
| Region | `us-east-1` |
| VPC / Subnet | `25bbdf-g23-vpc` / `25bbdf-g23-public-subnet` |
| EC2 Key Pair | Your existing key pair |

#### 3c. Submit the PySpark step

```
Step type : Spark application
Name      : 25bbdf-g23-preprocessing
Script    : s3://horus-25bbdf-g23-bucket/scripts/pyspark_job.py
Arguments : --bucket horus-25bbdf-g23-bucket
```

#### 3d. ⚠️ Terminate the cluster immediately after completion

> **Required for grading:** Screenshot the cluster in **Terminated** state.

```bash
# Verify processed output files
aws s3 ls s3://horus-25bbdf-g23-bucket/processed/ --recursive
```

**Expected output:**
```
processed/train.jsonl    ← 159,826 training samples
processed/val.jsonl
processed/test.jsonl
```

---

### Step 4 — Model Fine-Tuning (Google Colab T4 GPU)

Open `RUN_horus_osint_final_v3.ipynb` in Google Colab and follow these steps:

1. Set runtime: **Runtime → Change runtime type → T4 GPU**
2. Add the following to **Colab Secrets** (🔑 icon in left sidebar):
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
   - `HF_TOKEN` ← HuggingFace token with Llama-3 gated access
3. Run all cells in order

**What the notebook executes:**

| Stage | Action |
|---|---|
| Model Load | `unsloth/Meta-Llama-3-8B-Instruct-bnb-4bit` in 4-bit NF4 |
| PEFT Setup | LoRA adapters injected into all 7 attention/FFN projections |
| Data | 159,826 JSONL samples downloaded from S3, formatted to Llama-3 template |
| Training | SFTTrainer — 500 steps, ~15–25 min on T4 |
| Export | GGUF conversion: `q4_k_m` quantization → **4.92 GB** |
| Upload | GGUF pushed to `s3://horus-25bbdf-g23-bucket/models/` |

**Actual GGUF output path (Colab):**
```
/content/horus-llama3-osint_gguf/llama-3-8b-instruct.Q4_K_M.gguf
```

**S3 destination:**
```
s3://horus-25bbdf-g23-bucket/models/horus-llama3-osint-Q4_K_M.gguf
```

---

### Step 5 — Launch EC2 Instance

#### 5a. Launch via AWS Console

| Setting | Value |
|---|---|
| Name | `25bbdf-g23-ec2` |
| AMI | Ubuntu 22.04 LTS — Deep Learning OSS Nvidia Driver |
| Instance Type | `g4dn.xlarge` (1× NVIDIA T4, 16GB VRAM, 4 vCPU, 16GB RAM) |
| VPC | `25bbdf-g23-vpc` |
| Subnet | `25bbdf-g23-public-subnet` |
| Security Group | `25bbdf-g23-sg` |
| Storage | 100 GB gp3 |

#### 5b. Connect via SSH

```bash
ssh -i "your-key.pem" ubuntu@<ec2-public-ip>
```

---

### Step 6 — Model Deployment with Ollama

Run the following on the EC2 instance:

```bash
# 1. Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# 2. Verify installation
ollama --version

# 3. Install AWS CLI
sudo snap install aws-cli --classic

# 4. Download the fine-tuned GGUF from S3 (4.92 GB)
aws s3 cp s3://horus-25bbdf-g23-bucket/models/horus-llama3-osint-Q4_K_M.gguf \
    ./horus-llama3-osint.gguf

# 5. Create the Ollama Modelfile
echo "FROM ./horus-llama3-osint.gguf" > Modelfile

# 6. Register the model
ollama create horus-osint -f Modelfile

# 7. Launch interactive session (screenshot required)
ollama run horus-osint
```

#### 6a. Validate via REST API

```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "horus-osint",
    "prompt": "Identify the threat level of bombing incidents in Afghanistan in 2019.",
    "stream": false
  }'
```

> **Required screenshots:** `ollama run` terminal output + `curl` JSON response.

---

### Step 7 — Web Interface with OpenWebUI

```bash
# 1. Install Docker
sudo apt-get update && sudo apt-get install -y docker.io

# 2. Enable and start Docker
sudo systemctl enable docker && sudo systemctl start docker

# 3. Deploy OpenWebUI container
sudo docker run -d \
  -p 8080:8080 \
  --add-host=host.docker.internal:host-gateway \
  --restart always \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main

# 4. Confirm container is running
sudo docker ps
```

#### 7a. Access the interface

```
http://<ec2-public-ip>:8080
```

Select **horus-osint** from the model dropdown and begin a conversation.

> **Required screenshot:** Full browser view with model name visible and a sample OSINT query response.

---

### Step 8 — Teardown

```bash
# Terminate EC2 instance from AWS Console first, then destroy all Terraform resources:
terraform destroy -auto-approve
```

> ⚠️ Always destroy resources after submission to avoid exceeding the $90 credit limit.

---

## AWS Resource Naming Reference

| Resource | Name |
|---|---|
| VPC | `25bbdf-g23-vpc` |
| Internet Gateway | `25bbdf-g23-igw` |
| Public Subnet | `25bbdf-g23-public-subnet` |
| Route Table | `25bbdf-g23-rt` |
| Security Group | `25bbdf-g23-sg` |
| S3 Bucket | `horus-25bbdf-g23-bucket` |
| EMR Cluster | `25bbdf-g23-emr` |
| EC2 Instance | `25bbdf-g23-ec2` |
| Ollama Model | `horus-osint` |

---

## Security Group Rules

| Port | Protocol | Source | Justification |
|---|---|---|---|
| 22 | TCP | `0.0.0.0/0` | SSH administration access |
| 8080 | TCP | `0.0.0.0/0` | OpenWebUI public browser access |
| 11434 | TCP | `10.0.0.0/16` | Ollama API — **VPC-internal only** (no auth layer) |
| All | All | `0.0.0.0/0` (egress) | Package downloads, S3 access, Docker pulls |

---

## Hyperparameter Reference

| Hyperparameter | Value | Justification |
|---|---|---|
| Num Epochs (effective) | `0.03` | Computed: 500 steps ÷ (159,826 ÷ 8 eff. batch) |
| Learning Rate | `2e-4` | Standard stable starting point for AdamW PEFT |
| Batch Size | `2` | Optimised for T4 16GB VRAM ceiling |
| Gradient Accumulation | `4` | Effective batch = 8; stable gradient estimates |
| Effective Batch Size | `8` | 2 × 4 gradient accumulation steps |
| LoRA Rank (r) | `16` | Balances VRAM footprint with domain adaptation capacity |
| LoRA Alpha (α) | `32` | Standard 2× rank scaling for LoRA weight contribution |
| LoRA Dropout | `0.05` | Light regularisation to prevent format overfitting |
| Target Modules | `q/k/v/o/gate/up/down_proj` | Full attention + FFN coverage for domain shift |
| Optimizer | `adamw_8bit` | Memory-efficient optimizer native to Unsloth |
| Max Steps | `500` | Sufficient for OSINT instruction-format adaptation |
| Warmup Steps | `50` | Gradual LR ramp-up (10% of total steps) |
| LR Scheduler | `cosine` | Smooth decay; avoids destabilising sharp drops in PEFT |
| Max Sequence Length | `2,048` | Covers all OSINT Q&A pairs with margin |
| Training Quantization | `NF4 (4-bit)` | QLoRA — reduces VRAM from ~16 GB to ~6 GB |
| Export Quantization | `q4_k_m` | Best quality/size tradeoff for Ollama on g4dn.xlarge |

---

## AWS Cost Summary

| Service | Configuration | Estimated Cost |
|---|---|---|
| AWS EMR | 1× Master + 2× Core (m5.xlarge) · ~4 hours | ~$2.40 |
| AWS EC2 | 1× g4dn.xlarge · ~72 hours | ~$37.87 |
| AWS S3 | ~15 GB standard storage + requests | ~$0.35 |
| AWS VPC | IGW + data transfer | ~$2.00 |
| **Total** | | **~$42.62** |

> Budget remaining: **~$47.38** from the $100 AWS credit allocation.

---

## Fine-Tuning Results Summary

| Metric | Value |
|---|---|
| Training Samples | 159,826 |
| Training Duration | ~15–25 min (T4 GPU) |
| GGUF Model Size | 4.92 GB |
| Export Format | `q4_k_m` (4-bit K-quant medium) |
| S3 Model URI | `s3://horus-25bbdf-g23-bucket/models/horus-llama3-osint-Q4_K_M.gguf` |
| Base Model | `unsloth/Meta-Llama-3-8B-Instruct-bnb-4bit` |
| Fine-Tuning Method | QLoRA (PEFT) via Unsloth + HuggingFace TRL SFTTrainer |

---

*Built for CISC 886 — Cloud Computing, Queen's University.*
