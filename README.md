# 🦅 CISC 886: Horus-OSINT Cloud Assistant

**Queen's University — School of Computing**
**Student NetID:** 25BBDF

---

## Overview

Horus-OSINT is a cloud-based conversational chatbot designed to act as an Open-Source Intelligence (OSINT) and Global Threat Analyst. It leverages a fine-tuned `Meta-Llama-3-8B-Instruct` LLM trained on millions of records from the Global Terrorism Database (GTD) and GDELT, deployed entirely on AWS infrastructure.

### System Architecture

```
S3 (Raw GTD + GDELT)
       │
       ▼
EMR Cluster [25BBDF-emr]   ← PySpark preprocessing + EDA
(Status: Terminated)
       │
       ▼
S3 (Processed JSONL — train/val/test)
       │
       │                         ┌─────────────────────────┐
       ▼                         │  Google Colab (T4 GPU)  │
Fine-Tuning (Unsloth QLoRA).  ◄──┤  fine_tuning.ipynb      │
       │                         └─────────────────────────┘
       ▼
S3 (horus-llama3-osint-Q4_K_M.gguf)
       │
       ▼
EC2 [25BBDF-ec2] — g4dn.xlarge
  ├── Ollama  (port 11434 — internal only)
  └── OpenWebUI Docker  (port 8080 — public)
       │
       ▼
User Browser  →  http://<ec2-public-ip>:8080
```

---

## Repository Structure

```
.
├── main.tf              # Terraform — VPC, Subnet, IGW, SG, S3
├── pyspark_job.py       # PySpark preprocessing pipeline (EMR)
├── fine_tuning.ipynb    # Unsloth QLoRA fine-tuning notebook (Colab)
└── README.md            # This file
```

---

## Prerequisites

| Requirement | Version / Notes |
|---|---|
| AWS Account | Region: `us-east-1` |
| Terraform | >= 1.3 |
| Python | 3.10+ |
| Apache Spark | 3.x (on EMR) |
| Google Colab | T4 GPU runtime |
| HuggingFace Token | With access to `meta-llama/Meta-Llama-3-8B-Instruct` |
| AWS CLI | Configured with project credentials |

---

## Step-by-Step Replication

### Step 1 — Infrastructure Provisioning (VPC & Networking)

```bash
# Clone the repository
git clone https://github.com/MahmoudAlyosify/Horus-OSINT
cd horus-osint

# Initialise Terraform providers
terraform init

# Preview the resources that will be created
terraform plan

# Apply — creates VPC, Subnet, IGW, Route Table, Security Group, S3 Bucket
terraform apply -auto-approve
```

> **Note:** Save the output values (`vpc_id`, `subnet_id`, `security_group_id`, `s3_bucket_name`) — you will need them in subsequent steps.

---

### Step 2 — Upload Raw Data to S3

```bash
# Upload the merged GTD CSV to S3
aws s3 cp gtd_merged.csv s3://25BBDF-bucket/raw/gtd/gtd_merged.csv

# GDELT is accessed directly from the AWS Public Dataset — no upload needed:
# s3://gdelt-open-data/events/  (read directly in the PySpark script)
```

---

### Step 3 — Data Preprocessing on AWS EMR

#### 3a. Upload PySpark script to S3

```bash
aws s3 cp pyspark_job.py s3://25BBDF-bucket/scripts/pyspark_job.py
```

#### 3b. Launch EMR Cluster via AWS Console

| Setting | Value |
|---|---|
| Cluster Name | `25BBDF-emr` |
| EMR Release | emr-7.0.0 (Spark 3.5) |
| Master Node | 1 × m5.xlarge |
| Core Nodes | 2 × m5.xlarge |
| Region | us-east-1 |
| VPC / Subnet | `25BBDF-vpc` / `25BBDF-public-subnet` |
| EC2 Key Pair | Your key pair |

#### 3c. Add a Step to run the PySpark job

```
Step type : Spark application
Name      : 25BBDF-preprocessing
Script    : s3://25BBDF-bucket/scripts/pyspark_job.py
Arguments : --bucket 25BBDF-bucket
```

#### 3d. ⚠️ Terminate the cluster immediately after the step completes

> A screenshot of the cluster in **Terminated** state is **required** for Section 4 marks.

```bash
# Verify output files were written to S3
aws s3 ls s3://25BBDF-bucket/processed/ --recursive
```

Expected output files:
```
processed/train.jsonl
processed/val.jsonl
processed/test.jsonl
```

---

### Step 4 — Model Fine-Tuning (Google Colab)

1. Open `fine_tuning.ipynb` in Google Colab
2. Set runtime to **T4 GPU**: Runtime → Change runtime type → T4 GPU
3. Add the following to **Colab Secrets** (🔑 icon in left sidebar):
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
   - `HF_TOKEN` (HuggingFace token with Llama-3 access)
4. Run all cells in order
5. The notebook will:
   - Download `train.jsonl` from S3
   - Fine-tune Llama-3-8B-Instruct with QLoRA
   - Export the model to `horus-llama3-osint-Q4_K_M.gguf`
   - Upload the GGUF file to `s3://25BBDF-bucket/models/`

---

### Step 5 — Launch EC2 Instance

#### 5a. Launch via AWS Console

| Setting | Value |
|---|---|
| Name | `25BBDF-ec2` |
| AMI | Ubuntu 22.04 LTS (Deep Learning OSS Nvidia Driver) |
| Instance Type | `g4dn.xlarge` (1× NVIDIA T4, 16GB VRAM) |
| VPC | `25BBDF-vpc` |
| Subnet | `25BBDF-public-subnet` |
| Security Group | `25BBDF-sg` |
| Storage | 100 GB gp3 |

#### 5b. SSH into the instance

```bash
ssh -i "your-key.pem" ubuntu@<ec2-public-ip>
```

---

### Step 6 — Model Deployment with Ollama

Run all commands on the EC2 instance:

```bash
# 1. Install the Ollama LLM runner
curl -fsSL https://ollama.com/install.sh | sh

# 2. Verify Ollama is running
ollama --version

# 3. Install AWS CLI (if not present)
sudo snap install aws-cli --classic

# 4. Download the fine-tuned GGUF model from S3
aws s3 cp s3://25BBDF-bucket/models/horus-llama3-osint-Q4_K_M.gguf ./horus-llama3-osint.gguf

# 5. Create a Modelfile pointing to the GGUF binary
echo "FROM ./horus-llama3-osint.gguf" > Modelfile

# 6. Register the model with Ollama
ollama create horus-osint -f Modelfile

# 7. Run the model interactively (for screenshot)
ollama run horus-osint
```

#### 6a. Test via curl API call (from a second terminal tab)

```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "horus-osint",
    "prompt": "Identify the threat level of bombing incidents in Afghanistan in 2019.",
    "stream": false
  }'
```

> Screenshot both the `ollama run` terminal and the `curl` response for Section 6 of the report.

---

### Step 7 — Web Interface with OpenWebUI

Run on the EC2 instance:

```bash
# 1. Install Docker Engine
sudo apt-get update
sudo apt-get install -y docker.io

# 2. Start Docker service
sudo systemctl enable docker
sudo systemctl start docker

# 3. Deploy OpenWebUI — port 8080, auto-restart on reboot
sudo docker run -d \
  -p 8080:8080 \
  --add-host=host.docker.internal:host-gateway \
  --restart always \
  -v open-webui:/app/backend/data \
  --name open-webui \
  ghcr.io/open-webui/open-webui:main

# 4. Verify container is running
sudo docker ps
```

#### 7a. Access the web interface

Open a browser and navigate to:
```
http://<ec2-public-ip>:8080
```

Select `horus-osint` from the model dropdown and start a conversation.

> Screenshot the full browser UI with the model name visible and a sample conversation for Section 7 of the report.

---

### Step 8 — Teardown (After Submission)

```bash
# Terminate EC2 instance via console, then:
terraform destroy -auto-approve
```

> ⚠️ Destroy all resources after submission to avoid exhausting the $90 AWS credit.

---

## AWS Cost Summary

| Service | Configuration | Estimated Cost |
|---|---|---|
| AWS EMR | 1× Master + 2× Core (m5.xlarge) · ~4 hours | ~$2.40 |
| AWS EC2 | 1× g4dn.xlarge · ~72 hours (3 days dev/demo) | ~$37.87 |
| AWS S3 | Standard storage ~15 GB | ~$0.35 |
| AWS VPC | IGW + Data Transfer | ~$2.00 |
| **Total** | | **~$42.62** |

> Budget remaining: ~$47.38 from the $90 credit.

---

## Hyperparameter Table

| Hyperparameter | Value | Reasoning |
|---|---|---|
| Learning Rate | 2e-4 | Standard, stable starting point for PEFT using AdamW |
| Batch Size | 2 | Optimised for memory efficiency on Colab T4 16GB |
| Gradient Accumulation | 4 | Simulates effective batch size of 8 |
| LoRA Rank (r) | 16 | Balances VRAM usage with expressive power |
| LoRA Alpha | 32 | Standard scaling factor = 2 × rank |
| Optimizer | adamw_8bit | Memory-efficient optimizer from Unsloth |
| Max Steps | 500 | Sufficient for OSINT format adaptation |
| Warmup Steps | 50 | Prevents early training instability |
| LR Scheduler | cosine | Smooth decay over training |
| Max Sequence Length | 2048 | Covers all OSINT Q&A pairs |
| Export Quantization | q4_k_m | Best quality/size tradeoff for Ollama |

---

## Resource Naming Reference

| Resource | Name |
|---|---|
| VPC | `25BBDF-vpc` |
| Internet Gateway | `25BBDF-igw` |
| Public Subnet | `25BBDF-public-subnet` |
| Route Table | `25BBDF-rt` |
| Security Group | `25BBDF-sg` |
| S3 Bucket | `25BBDF-bucket` |
| EMR Cluster | `25BBDF-emr` |
| EC2 Instance | `25BBDF-ec2` |
| Ollama Model | `horus-osint` |
