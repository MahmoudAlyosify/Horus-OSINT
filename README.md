<h1 align="left">
  <img src="https://github.com/user-attachments/assets/07ed572b-1dee-4cf2-b30c-2f35ca931d61" width="40" style="vertical-align: middle;"/>
  CISC 886: Horus-OSINT Cloud Assistant
</h1>

**Queen's University — School of Computing**
**Group 23 | AWS NetID Prefix:** `25bbdf-g23`

| **Team Members** | Mahmoud Alyosify · Sondos Omar · Mirna Embaby |
|---|---|
| **GitHub Repository** | [github.com/MahmoudAlyosify/Horus-OSINT](https://github.com/MahmoudAlyosify/Horus-OSINT) |
| **HuggingFace Model** | [huggingface.co/mahmoudalyosify/Horus-OSINT](https://huggingface.co/mahmoudalyosify/Horus-OSINT) |

---

## Overview

Horus-OSINT is a cloud-based conversational chatbot designed to act as an Open-Source Intelligence (OSINT) and Global Threat Analyst. It leverages a fine-tuned `Meta-Llama-3-8B-Instruct` LLM trained on 159,826 structured records derived from the Global Terrorism Database (GTD) and GDELT, deployed entirely on AWS infrastructure.

---

### Video Demo

https://github.com/user-attachments/assets/bea69010-ebb0-4769-8837-5be100bca8b2

---
## Quickstart (TL;DR)

```
Step 1 → terraform apply          # Provision VPC, Subnet, SG, S3
Step 2 → aws s3 cp gtd_merged.csv # Upload raw data to S3
Step 3 → EMR Step (PySpark)       # Preprocess 20M+ records → JSONL
Step 4 → Colab Notebook (T4 GPU)  # QLoRA fine-tune → upload GGUF to S3
Step 5 → EC2 (t3.xlarge)          # Ollama + HORUS Custom UI (Nginx/Docker)
Step 6 → terraform destroy        # Teardown — avoid cost overrun
```

> **Full commands for each step are in the sections below.**

---

## System Architecture

```
S3 (Raw GTD + GDELT)
       │
       ▼
EMR Cluster [25bbdf-horus-emr-cluster]   ← PySpark preprocessing + EDA
(Status: Terminated immediately after use)
       │
       ▼
S3 (Processed JSONL — train/val/test)
       │
       │                         ┌────────────────────────────────┐
       ▼                         │  Google Colab (T4 GPU).        │
Fine-Tuning (Unsloth QLoRA).  ◄──┤  horus_osint_fine_tuning.ipynb │
       │                         └────────────────────────────────┘
       ▼
S3 (horus-llama3-osint-Q4_K_M.gguf)
       │
       ▼
EC2 [25bbdf-g23-ec2] — t3.xlarge
  ├── Ollama         (port 11434 — VPC-internal only)
  └── HORUS UI       (Dockerized Nginx, port 80 — public)
       │
       ▼
User Browser  →  http://<ec2-public-ip>
```
<img width="2141" height="1702" alt="v3 drawio" src="https://github.com/user-attachments/assets/dd049e42-720d-49e6-bc71-49fea774e472" />

---

## Repository Structure

```
.
├── main.tf                         # Terraform — VPC, Subnet, IGW, SG, S3
├── pyspark_job.py                  # PySpark preprocessing pipeline (EMR)
├── horus_osint_fine_tuning.ipynb   # Unsloth QLoRA fine-tuning notebook (Colab)
└── README.md                       # This file
```

---

## Prerequisites

| Requirement | Version / Notes |
|---|---|
| AWS Account | Region: `us-east-1` (N. Virginia) |
| Terraform | >= 1.3 |
| Python | 3.10+ |
| Apache Spark | 3.x (on EMR — no local install needed) |
| Google Colab | T4 GPU runtime (free tier sufficient) |
| HuggingFace Token | With access to `meta-llama/Meta-Llama-3-8B-Instruct` |
| AWS CLI | Configured with project credentials (`aws configure`) |

### Python Dependencies (for local use / inspection)

```bash
pip install torch transformers datasets trl peft accelerate bitsandbytes boto3 unsloth
```

> **Note:** The fine-tuning notebook is designed for **Google Colab (T4 GPU)**. To reproduce locally, you need a CUDA-compatible GPU with ≥16 GB VRAM and the dependencies above. All notebook cells include inline installation commands — Colab is the recommended environment.

---

## IAM Requirements

Ensure your AWS IAM user or role has the following policies attached before running any step:

| Policy | Purpose |
|---|---|
| `AmazonS3FullAccess` | Upload/download data and model artefacts |
| `AmazonEC2FullAccess` | Launch and manage the t3.xlarge instance |
| `AmazonEMRFullAccess` | Create and terminate the EMR cluster |
| `IAMFullAccess` (or scoped) | Attach instance profile to EMR/EC2 |

EMR also requires a **service role** (`AmazonEMR-ServiceRole`) and an **EC2 instance profile** (`AmazonEMR-InstanceProfile`) — these are created automatically when you launch EMR via the console for the first time.

---

## Dataset Sources

| Dataset | Description | Source |
|---|---|---|
| **Global Terrorism Database (GTD)** | 200,000+ verified terrorist incidents (1970–2020) — attack type, perpetrator, target, casualties | [start.umd.edu/gtd](https://www.start.umd.edu/data-tools/GTD/)|
| **GDELT Event Database** | Real-time geopolitical event database with Goldstein scale & tone scores | [gdeltproject.org](https://www.gdeltproject.org/) · AWS Open Data: `s3://gdelt-open-data/events/` |

Combined raw records: **20M+** → Post-ETL training samples: **159,826**

---

## Step-by-Step Replication

### Step 1 — Infrastructure Provisioning (Terraform)

```bash
# Clone the repository
git clone https://github.com/MahmoudAlyosify/Horus-OSINT
cd Horus-OSINT

# Initialise Terraform providers
terraform init

# Preview resources before creation
terraform plan

# Apply — creates VPC, Subnet, IGW, Route Table, Security Group, S3 Bucket
terraform apply -auto-approve
```

> Save the output values (`vpc_id`, `subnet_id`, `security_group_id`, `s3_bucket_name`) — required for Steps 3 and 5.

**Resources created (all prefixed `25bbdf-g23-`):**

| Resource | Name |
|---|---|
| VPC | `25bbdf-g23-vpc` |
| Internet Gateway | `25bbdf-g23-igw` |
| Public Subnet | `25bbdf-g23-public-subnet` |
| Route Table | `25bbdf-g23-rt` |
| Security Group | `25bbdf-g23-sg` |
| S3 Bucket | `horus-25bbdf-g23-bucket` |

---

### Step 2 — Upload Raw Data to S3

```bash
# Download GTD merged CSV from the START Consortium (requires registration)
# Then upload to S3:
aws s3 cp gtd_merged.csv s3://horus-25bbdf-g23-bucket/raw/gtd/gtd_merged.csv

# GDELT is accessed directly from the AWS Public Dataset — no upload needed:
# s3://gdelt-open-data/events/  (read directly in the PySpark script)
```

---

### Step 3 — Data Preprocessing on AWS EMR

#### 3a. Upload PySpark script to S3

```bash
aws s3 cp pyspark_job.py s3://horus-25bbdf-g23-bucket/scripts/pyspark_job.py
```

#### 3b. Launch EMR Cluster via AWS Console

| Setting | Value |
|---|---|
| Cluster Name | `25bbdf-horus-emr-cluster` |
| EMR Release | emr-7.13.0 |
| Region | us-east-1 |
| Master Node | 1 × m5.xlarge |
| Core Nodes | 1 × m5.xlarge (Maximum: 1 core instance) |
| VPC / Subnet | `25bbdf-g23-vpc` / `25bbdf-g23-public-subnet` |
| EC2 Key Pair | `Horus-key-pair` |

#### 3c. Add a Step to run the PySpark job

```
Step type : Spark application
Name      : 25bbdf-g23-preprocessing
Script    : s3://horus-25bbdf-g23-bucket/scripts/pyspark_job.py
Arguments : --bucket horus-25bbdf-g23-bucket
```

#### 3d. Terminate the cluster immediately after the step completes

```bash
# Verify output files were written to S3
aws s3 ls s3://horus-25bbdf-g23-bucket/processed/ --recursive
```

Expected output:
```
processed/train.jsonl      (~150k samples, 95%)
processed/val.jsonl        (~5k samples,   2.5%)
processed/test.jsonl       (~5k samples,   2.5%)
```

---

### Step 4 — Model Fine-Tuning (Google Colab)

1. Open `horus_osint_fine_tuning.ipynb` in Google Colab
2. Set runtime: **Runtime → Change runtime type → T4 GPU**
3. Add the following to **Colab Secrets** (icon in left sidebar):
   - `AWS_ACCESS_KEY_ID`
   - `AWS_SECRET_ACCESS_KEY`
   - `HF_TOKEN` (HuggingFace token with Llama-3 access)
4. Run all cells in order

The notebook will:
- Download `train.jsonl` from S3
- Fine-tune `Meta-Llama-3-8B-Instruct` with QLoRA (4-bit NF4, Unsloth)
- Export the merged model to `horus-llama3-osint-Q4_K_M.gguf`
- Upload the GGUF to `s3://horus-25bbdf-g23-bucket/models/`
- Publish adapter weights to HuggingFace Hub

---

### Step 5 — EC2 Deployment (Ollama + HORUS Custom UI)

#### 5a. Launch EC2 Instance via AWS Console

| Setting | Value |
|---|---|
| Name | `25bbdf-g23-ec2` |
| AMI | Ubuntu 22.04 LTS (Deep Learning OSS Nvidia Driver) |
| Instance Type | `t3.xlarge` |
| VPC | `25bbdf-g23-vpc` |
| Subnet | `25bbdf-g23-public-subnet` |
| Security Group | `25bbdf-g23-sg` |
| Storage | 30 GB gp3 |
| EC2 Key Pair | `Horus-key-pair` |

Deploy the instance into `25bbdf-g23-vpc`, then SSH in:

```bash
ssh -i "Horus-key-pair.pem" ubuntu@<25bbdf-g23-ec2-public-ip>
```

#### 5b. Install Ollama and Configure CORS

To allow the client-side UI to call the Ollama API directly, CORS and host binding must be configured via a systemd override — not just an environment variable — so the setting persists across reboots.

```bash
# Install the Ollama LLM runner
curl -fsSL https://ollama.com/install.sh | sh

# Configure CORS and host binding via systemd override
sudo mkdir -p /etc/systemd/system/ollama.service.d
echo -e "[Service]\nEnvironment=\"OLLAMA_HOST=0.0.0.0\"\nEnvironment=\"OLLAMA_ORIGINS=*\"" \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf

# Reload daemon and restart Ollama
sudo systemctl daemon-reload
sudo systemctl restart ollama

# Verify it is running and listening on all interfaces
sudo systemctl status ollama
```

#### 5c. Load the Fine-Tuned Model

```bash
# Install AWS CLI
sudo snap install aws-cli --classic

# Download GGUF from S3
aws s3 cp s3://horus-25bbdf-g23-bucket/models/horus-llama3-osint-Q4_K_M.gguf \
    ./horus-llama3-osint.gguf

# Create Modelfile and register with Ollama
echo "FROM ./horus-llama3-osint.gguf" > Modelfile
ollama create horus-osint -f Modelfile

# Verify model is registered
ollama list
```

#### 5d. Deploy HORUS Command Center UI (Dockerized Nginx)

Instead of a generic off-the-shelf interface, a bespoke HTML/JS Single Page Application was engineered and deployed to render streaming Markdown outputs as structured "Intelligence Report Cards" matching the HORUS brand identity.

```bash
# Install Docker
sudo apt-get update && sudo apt-get install -y docker.io
sudo systemctl enable docker && sudo systemctl start docker

# Create web directory and transfer UI assets (index.html, logo.png)
mkdir -p ~/horus-ui
# scp index.html logo.png ubuntu@<ec2-ip>:~/horus-ui/

# Deploy lightweight Nginx server on port 80 — auto-restart on reboot
sudo docker run -d \
  -p 80:80 \
  --name horus-custom-ui \
  --restart always \
  -v ~/horus-ui:/usr/share/nginx/html:ro \
  nginx:alpine

# Verify container is running
sudo docker ps
```

Access the interface at:

```
http://<25bbdf-g23-ec2-public-ip>
```

#### 5e. Test via cURL API

```bash
curl http://localhost:11434/api/generate \
  -d '{
    "model": "horus-osint",
    "prompt": "Identify the threat level of bombing incidents in Afghanistan in 2019.",
    "stream": false
  }'
```

---

### Step 6 — Teardown (After Submission)

```bash
# Terminate EC2 instance via console first, then:
terraform destroy -auto-approve
```

> **Always run `terraform destroy` after submission** to prevent exhausting the AWS credit.

---

## Enterprise-Grade Custom Web Interface

To demonstrate the full operational capabilities of the fine-tuned OSINT LLM, the team engineered and deployed a custom, production-ready Single Page Application (SPA) tailored to the HORUS cyber-intelligence brand identity. Hosted via a Dockerized Nginx server on the AWS EC2 instance, the interface connects directly to the Ollama backend API with custom CORS configuration and dynamic streaming rendering — converting raw Markdown into formatted Intelligence Report Cards in real time. Both desktop and mobile responsiveness were validated.

---

## Advanced Prompt Engineering & Model Evaluation

The fine-tuned model was stress-tested with two multi-dataset correlation prompts designed to validate that GDELT geopolitical context and GTD tactical incident data were correctly learned and can be synthesized in a single response:

**Phase 1 — Deep Strategic Assessment:**

> *"Execute a strategic OSINT threat assessment for Iraq covering the year 2014. Correlate GDELT political instability metrics with GTD terror incident data. Focus on identifying primary target demographics, dominant attack modalities used by insurgent groups, and the overall threat severity level. Present the findings strictly in the HORUS Intelligence Report format."*

**Phase 2 — Tactical Field Retrieval:**

> *"Generate a HORUS Intelligence Report for Syria, 2015. Correlate GDELT instability with GTD tactical attacks, highlighting weapon modalities and target demographics."*

In both evaluations, the model adhered to strict HORUS Intelligence Report Card formatting while accurately synthesizing multi-source threat demographics — confirming that the GTD+GDELT join performed during PySpark preprocessing successfully transferred into the model's domain knowledge. The seamless deployment of this architecture demonstrates the team's ability to bridge large-scale Big Data pipelines, advanced LLM fine-tuning, and scalable cloud infrastructure into a unified, end-to-end intelligence product.

---

## 8. Cloud Infrastructure Cost & FinOps Analysis

**Disclaimer on Cost Estimation:** Due to the strict IAM policy restrictions enforced on our federated university AWS account, direct access to the AWS Billing and Cost Management console is restricted. Consequently, the cost metrics presented below are rigorous manual estimates. These calculations are derived from the official AWS on-demand pricing for the **us-east-1 (N. Virginia)** region mapped against our exact resource provisioning timelines.

To ensure strict compliance with the **$100.00 USD hard budget cap**, the team employed aggressive FinOps governance. This included terminating the EMR cluster immediately post-preprocessing and offloading the VRAM-intensive LoRA fine-tuning process entirely to a free-tier Google Colab (T4 GPU) environment.

**Estimated AWS Cost Breakdown (USD):**

| AWS Service | Resource Configuration | Unit Price (us-east-1) | Active Usage | Approx. Cost (USD) |
|---|---|---|---|---|
| **Amazon S3** | Standard Storage (~15 GB) | $0.023 / GB / month | ~15 GB | ~$1.50 |
| **Amazon EMR** | 2× m5.xlarge (Master + Core) | $0.192 / hour per node | ~5 hours | ~$2.00 |
| **Amazon EC2** | 1× t3.xlarge (30 GB gp3) | $0.1664 / hour | ~6 hours | ~$1.00 |
| **VPC / Network** | Data Transfer & IGW | Variable | — | ~$0.35 |
| **Google Colab** | T4 GPU (Fine-tuning) | $0.00 | Free Tier | $0.00 |
| **TOTAL** | | | | **~$4.85 USD** |

---

## Hyperparameter Table

| Hyperparameter | Value | Reasoning |
|---|---|---|
| Num Epochs (effective) | 0.03 | Computed as MAX\_STEPS / (dataset\_size / effective\_batch\_size); reported per rubric |
| Learning Rate | 2e-4 | Standard, stable starting point for PEFT using AdamW |
| Batch Size | 2 | Optimised for memory efficiency on Colab T4 16 GB VRAM |
| Gradient Accumulation | 4 | Simulates effective batch size of 8 for stable gradient updates |
| Effective Batch Size | 8 | 2 × 4 = 8; provides stable gradient estimates |
| LoRA Rank (r) | 16 | Balances VRAM usage with expressive power needed for domain adaptation |
| LoRA Alpha | 32 | Standard scaling factor = 2 × rank applied to LoRA weights |
| LoRA Dropout | 0.05 | Light regularisation to prevent overfitting on OSINT format |
| Optimizer | adamw\_8bit | Highly memory-efficient optimizer provided natively by Unsloth |
| Max Steps | 500 | Sufficient for the model to adapt to the OSINT instruction format |
| Warmup Steps | 50 | Gradual LR ramp-up (10% of training) to prevent early instability |
| LR Scheduler | cosine | Smooth decay avoids sharp LR drops that destabilise PEFT training |
| Max Sequence Length | 2048 | Covers longest OSINT Q&A pairs with comfortable margin |
| Quantization (training) | NF4 (4-bit) | QLoRA base quantization — reduces VRAM from ~16 GB to ~6 GB |
| Quantization (export) | q4\_k\_m | Best quality/size tradeoff for Ollama deployment on EC2 |

---

## Resource Naming Reference

| Resource | Name |
|---|---|
| VPC | `25bbdf-g23-vpc` |
| Internet Gateway | `25bbdf-g23-igw` |
| Public Subnet | `25bbdf-g23-public-subnet` |
| Route Table | `25bbdf-g23-rt` |
| Security Group | `25bbdf-g23-sg` |
| S3 Bucket | `horus-25bbdf-g23-bucket` |
| EMR Cluster | `25bbdf-horus-emr-cluster` |
| EC2 Instance | `25bbdf-g23-ec2` |
| Ollama Model | `horus-osint` |
