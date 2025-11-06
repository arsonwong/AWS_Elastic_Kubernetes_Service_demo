# 🧩 AWS Fargate Batch Workflow Automation

This repository provides a modular Python-based automation pipeline to **build, deploy, and execute distributed batch jobs** on **AWS EKS with Fargate**, using **S3** for data storage and **ECR** for container management.  It runs on demo dummy data and dummy job, but you can easily alter those for real work.

---

## 🚀 Overview

This workflow automates:
1. **Docker image build & push** to AWS ECR.
2. **EKS cluster setup** (with Fargate and IRSA integration).
3. **VPC endpoint configuration** for secure internal communication.
4. **S3 data upload and management**.
5. **Kubernetes job orchestration** for batch simulations.
6. **Result collection** from S3 after execution.

---

## ⚙️ Prerequisites

### 🧰 Required Tools
Ensure these are installed and correctly referenced in `config.toml`:
- [AWS CLI v2](https://aws.amazon.com/cli/)
- [Docker Desktop](https://docs.docker.com/desktop/)
- [eksctl](https://github.com/eksctl-io/eksctl)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- Python ≥ 3.11

### Python Dependencies
```bash
pip install -r requirements.txt
```

### 🔑 AWS Setup
You must have:
- AWS account with admin or sufficient IAM permissions.
- AWS SSO profile configured via:
  ```bash
  aws configure sso
  ```
- You can view the profile name with:
  ```bash
  aws configure list-profiles
  ```

- You'll need appropriate access to create and manage:
  - ECR repositories
  - EKS clusters
  - S3 buckets
  - IAM roles/policies

---

## 🧩 Step-by-Step Execution

### **Step 1: Build and Push Docker Image**
Run:
```bash
python step01_build_docker_image_and_push.py
```
This builds your container and uploads it to **Amazon ECR**.

---

### **Step 2: Create EKS Cluster, Fargate Profile, and S3 Bucket**
Run:
```bash
python step02_fargate_EKS_cluster_S3_bucket_setup.py
```
This script:
- Ensures AWS SSO session is active.
- Creates or verifies an EKS cluster with Fargate support.
- Sets up the IAM roles (IRSA) and Kubernetes service accounts.
- Creates an S3 bucket and attaches appropriate read/write policies.

---

### **Step 3: Configure Private Network Endpoints**
Run:
```bash
python step03_network_endpoints_setup.py
```
This configures **VPC endpoints** for **S3** (Gateway) and **STS** (Interface), ensuring private, internal-only access for all EKS resources.

---

### **Step 4: Upload Input Data**
Run:
```bash
python step04_upload_data.py
```
Uploads local data shards from `dummy files` to the S3 bucket’s `/input/` directory. 

---

### **Step 5: Launch Fargate Batch Job and Download Results**
Run:
```bash
python step05_run_pods_and_download_results.py
```
This step:
- Deploys an **Indexed Kubernetes Job** to Fargate.
- Streams job logs in real-time with per-shard progress.
- Downloads the resulting output from S3’s `/output/` folder to your local `dummy files/out/` directory.

---

### **Step 6: Optional Cleanup**
Run:
```bash
python step06_optional_cleanup.py
```
You can choose to delete the S3 bucket, ECR repo, and EKS cluster

---
