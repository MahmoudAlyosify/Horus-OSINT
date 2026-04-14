# =============================================================================
#  CISC 886 – Cloud Computing | Queen's University
#  Horus-OSINT: VPC & Networking Infrastructure
#  Student NetID : 25BBDF
#
#  Usage:
#    1. Replace <YOUR_LOCAL_IP> below with your actual IP (https://checkip.amazonaws.com)
#    2. terraform init
#    3. terraform apply -auto-approve
#    4. terraform destroy  (after submission — avoid costs)
# =============================================================================

terraform {
  required_version = ">= 1.3"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# -----------------------------------------------------------------------------
#  Provider — us-east-1 is the standardised project region
# -----------------------------------------------------------------------------
provider "aws" {
  region = "us-east-1"
}

# =============================================================================
#  LOCALS — all names follow the mandatory <netID>-<resource> policy
# =============================================================================
locals {
  netid  = "25bbdf"
  prefix = "25bbdf"    # Every AWS resource name starts with this

  # CIDR blocks — /16 gives 65,536 addresses for the VPC;
  # /24 gives 256 addresses for the single public subnet.
  vpc_cidr    = "10.0.0.0/16"
  subnet_cidr = "10.0.1.0/24"
}

# =============================================================================
#  1. VPC
#  Justification: A custom VPC provides network isolation — all project
#  resources are contained within a private, dedicated address space,
#  preventing IP conflicts with other students on the shared AWS account.
#  enable_dns_hostnames=true is required so EC2 instances receive public DNS
#  names, which Ollama and OpenWebUI need for external connectivity.
# =============================================================================
resource "aws_vpc" "main" {
  cidr_block           = local.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name    = "${local.prefix}-vpc"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

# =============================================================================
#  2. Internet Gateway
#  Justification: Allows bidirectional traffic between the public subnet and
#  the internet. Required for (a) SSH access to EC2, (b) users accessing
#  OpenWebUI on port 8080, and (c) EC2 downloading packages and the GGUF model
#  from S3 via the internet (no VPC endpoint configured to stay within budget).
# =============================================================================
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name    = "${local.prefix}-igw"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

# =============================================================================
#  3. Public Subnet
#  Justification: A single public subnet in us-east-1a is sufficient for this
#  project — multi-AZ redundancy is unnecessary for a course prototype.
#  map_public_ip_on_launch=true ensures EC2 instances automatically receive a
#  public IP, enabling SSH and browser access without an Elastic IP.
# =============================================================================
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.subnet_cidr
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = true

  tags = {
    Name    = "${local.prefix}-public-subnet"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

# =============================================================================
#  4. Route Table & Association
#  Justification: The default VPC route table is not usable (we created a new
#  VPC). This route table adds a default route (0.0.0.0/0) pointing to the
#  Internet Gateway, making the subnet "public" — all outbound traffic from
#  EC2 can reach the internet for downloads and S3 access.
# =============================================================================
resource "aws_route_table" "rt" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name    = "${local.prefix}-rt"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

resource "aws_route_table_association" "public_assoc" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.rt.id
}

# =============================================================================
#  5. Security Group
#  Justification (per port — required by rubric):
#
#  Port 22  (SSH)     → 0.0.0.0/0 : Allows remote administration of the EC2
#                       instance. In production this would be restricted to a
#                       single IP; for course purposes open access is acceptable.
#
#  Port 8080 (WebUI)  → 0.0.0.0/0 : OpenWebUI runs on port 8080 inside Docker.
#                       Open to the internet so course evaluators can access
#                       the chat interface from any browser.
#
#  Port 11434 (Ollama)→ 10.0.0.0/16 ONLY : The Ollama REST API must NOT be
#                       exposed publicly — it has no authentication layer.
#                       Restricting to the VPC CIDR means only the OpenWebUI
#                       Docker container (on the same host / same VPC) can
#                       query it, preventing unauthorized LLM access.
#
#  Egress all         → 0.0.0.0/0 : Required for EC2 to download packages
#                       (apt, pip), pull the Docker image, and download the
#                       GGUF model from S3.
# =============================================================================
resource "aws_security_group" "sg" {
  name        = "${local.prefix}-sg"
  description = "Horus-OSINT: SSH + OpenWebUI access, internal-only Ollama API"
  vpc_id      = aws_vpc.main.id

  # ── SSH — remote management
  ingress {
    description = "SSH Access"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # ── OpenWebUI — user-facing chat interface
  ingress {
    description = "OpenWebUI Browser Access"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # ── Ollama API — restricted to VPC internal traffic only
  ingress {
    description = "Ollama API (VPC-internal only)"
    from_port   = 11434
    to_port     = 11434
    protocol    = "tcp"
    cidr_blocks = [local.vpc_cidr]   # 10.0.0.0/16 — NOT the internet
  }

  # ── All outbound traffic
  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${local.prefix}-sg"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

# =============================================================================
#  6. S3 Bucket (Data Lake + Model Storage)
#  Justification: Centralised object storage for raw data, processed JSONL,
#  and the GGUF model weights. S3 decouples storage from compute — the EMR
#  cluster and EC2 instance can both access data independently.
# =============================================================================
resource "aws_s3_bucket" "datalake" {
  bucket        = "horus-${local.prefix}-bucket"
  force_destroy = true   # Allows terraform destroy to empty the bucket

  tags = {
    Name    = "horus-${local.prefix}-bucket"
    Project = "Horus-OSINT"
    NetID   = local.netid
  }
}

resource "aws_s3_bucket_versioning" "datalake" {
  bucket = aws_s3_bucket.datalake.id
  versioning_configuration {
    status = "Disabled"   # No versioning needed — saves storage cost
  }
}

# Block all public access to S3 — data accessed via EC2 role, not public URLs
resource "aws_s3_bucket_public_access_block" "datalake" {
  bucket                  = aws_s3_bucket.datalake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# =============================================================================
#  OUTPUTS — printed after terraform apply
# =============================================================================
output "vpc_id" {
  description = "Custom VPC ID"
  value       = aws_vpc.main.id
}

output "subnet_id" {
  description = "Public subnet ID (use when launching EMR / EC2)"
  value       = aws_subnet.public.id
}

output "security_group_id" {
  description = "Security group ID (attach to EC2 instance)"
  value       = aws_security_group.sg.id
}

output "s3_bucket_name" {
  description = "S3 data lake bucket name"
  value       = aws_s3_bucket.datalake.bucket
}

output "s3_bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.datalake.arn
}
