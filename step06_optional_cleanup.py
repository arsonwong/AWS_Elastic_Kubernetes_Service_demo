# Skip this if you want to retain your AWS resources
# Rerun if you want to completely remove EKS, ECR, and S3 resources to avoid costs.

import os, subprocess
from pathlib import Path
import tomllib
from utilities import *

# ───────────────────────────────
# Load configuration
# ───────────────────────────────
config_path = Path("config.toml")

with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
ECR_REPO = config["AWS_profile"]["ECR_REPO"]
CLUSTER = config["AWS_profile"]["CLUSTER"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]

AWS = config["paths"]["AWS"]
EKSCTL = config["paths"]["EKSCTL"]

ensure_sso_logged_in(AWS, PROFILE)

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""

REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE],
    text=True
).strip()

ACCOUNT_ID = subprocess.check_output(
    [AWS, "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
    text=True
).strip()

bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"

confirm = input(f"⚠️  Type 'Y' to confirm deletion of S3 Bucket: {bucket_name}").strip()
if confirm == "Y":
    print("\nRemoving S3 bucket contents and bucket...")
    sh([AWS, "s3", "rm", f"s3://{bucket_name}/", "--recursive"], check=False)
    sh([AWS, "s3api", "delete-bucket", "--bucket", bucket_name, "--region", REGION], check=False)
    print("✔ S3 bucket deleted (if existed).")

confirm = input(f"⚠️  Type 'Y' to confirm deletion of ECR repository: {ECR_REPO}").strip()
if confirm == "Y":
    print("\nDeleting ECR repository...")
    sh([AWS, "ecr", "delete-repository", "--repository-name", ECR_REPO, "--region", REGION, "--force"], check=False)
    print("✔ ECR repository deleted (if existed).")

confirm = input(f"⚠️  Type 'Y' to confirm deletion of EKS cluster: {CLUSTER}").strip()
if confirm == "Y":
    print("\nDeleting EKS cluster (this may take a few minutes)...")
    sh([EKSCTL, "delete", "cluster", "--name", CLUSTER, "--region", REGION, "--profile", PROFILE], check=False)
    print("✔ EKS cluster deletion command issued.")

print("\n✅ Cleanup complete. Check AWS Console for any remaining resources.")
