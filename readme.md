# 🧩 AWS Fargate Batch & EKS Workflow Automation

This repository provides a modular, Python-based automation pipeline to **build, deploy, and execute distributed batch jobs** on either:

- **AWS Batch (Fargate-based)**   
- **Amazon EKS (Fargate-based Kubernetes)** 

Both use **Amazon S3** for data storage and **ECR** for container management.  
The repo includes **demo dummy data and jobs**, which you can easily replace for real workloads.

---

## 🚀 Overview

Each workflow automates:
1. **Docker image build & push** → AWS ECR  
2. **Compute environment setup** → Batch *or* EKS with Fargate  
3. **Networking (VPC endpoints)** → Secure private access  
4. **S3 bucket management** → `/input/` and `/output/`  
5. **Job submission & monitoring**  
6. **Result download from S3**

---

## ⚙️ Prerequisites

### 🧰 Required Tools
Make sure these tools are installed and configured in `config.toml`:
- [AWS CLI v2](https://aws.amazon.com/cli/)
- [Docker Desktop](https://docs.docker.com/desktop/)
- Python ≥ 3.11

If you're going the Amazon EKS route
- [kubectl](https://kubernetes.io/docs/tasks/tools/)     
- [eksctl](https://github.com/eksctl-io/eksctl)

### Python Dependencies
```bash
pip install -r requirements.txt
```

### 🔑 AWS Setup
- Configure an AWS SSO profile:
  ```bash
  aws configure sso
  ```
- Check your available profiles:
  ```bash
  aws configure list-profiles
  ```
- Ensure IAM permissions to create/manage:
  - ECR repositories  
  - EKS clusters or AWS Batch compute environments  
  - S3 buckets  
  - IAM roles/policies  

---

## 🧩 Repository Structure

```
AWS batch implementation/
│
├── step01_build_docker_image_and_push.py
├── step02_batch_env_S3_bucket_setup.py
├── step03_network_endpoints_setup.py
├── step04_upload_data.py
├── step05_submit_batch_array_and_download.py
└── step06_batch_cleanup.py

EKS cluster implementation/
│
├── step01_build_docker_image_and_push.py
├── step02_fargate_EKS_cluster_S3_bucket_setup.py
├── step03_network_endpoints_setup.py
├── step04_upload_data.py
├── step05_run_pods_and_download_results.py
└── step06_optional_cleanup.py
```

Each directory represents a **fully independent automation path** — choose one (Batch *or* EKS).

---

## 🧹 Cleanup
You can safely tear down all resources:
- Delete S3 buckets, Batch environments, ECR repos, and EKS clusters.
- Use the provided step06 cleanup scripts.


## 🧭 Summary

| Feature | AWS Batch | Amazon EKS |
|----------|------------|------------|
| **Management Style** | Fully managed by AWS | User-managed Kubernetes |
| **Best For** | Simple scalable batch runs | Complex workflows needing Kubernetes control |
| **Launch Type** | Fargate (no EC2 required) | Fargate (serverless pods) |
| **Job Type** | AWS Batch Array Job | Indexed Kubernetes Job |
| **Monitoring** | CloudWatch logs | `kubectl` logs |
| **Cleanup** | Batch cleanup script | EKS cleanup script |

---
