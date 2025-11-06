# Skip this if already done once

import os, subprocess
from pathlib import Path
import tomllib
from utilities import *
import json
import time
import boto3

config_path = Path("config.toml")

with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
ECR_REPO = config["AWS_profile"]["ECR_REPO"]
IMAGE_TAG = config["AWS_profile"]["IMAGE_TAG"]
CLUSTER = config["AWS_profile"]["CLUSTER"]
FARGATE_NS = config["AWS_profile"]["FARGATE_NS"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
GSA_ROLE = config["AWS_profile"]["GSA_ROLE"]
KSA = config["AWS_profile"]["KSA"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]
# Docker Desktop for building and pushing containerized applications up to the cloud
DOCKER  = config["paths"]["DOCKER"]
# eskctl command-line utility for creating and managing EKS clusters (infrastructure and cluster setup)
EKSCTL = config["paths"]["EKSCTL"]
# kubectl command-line tool for interacting with Kubernetes clusters (Works inside the cluster (Pods, Jobs, Deployments, Services, etc.))
KUBECTL = config["paths"]["KUBECTL"]

ALLOW_CFN_CLEANUP = False   # set True only when you *want* the script to clean failed stacks

# SSO login
ensure_sso_logged_in(AWS, PROFILE)

REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE],
    text=True
).strip()
session = boto3.Session(profile_name=PROFILE, region_name=REGION)
REGION = session.region_name or REGION
if not REGION:
    raise RuntimeError(
        f"No region resolved for profile '{PROFILE}'. "
        f"Set region in ~/.aws/config or include AWS_profile.REGION in config.toml."
    )

# Ensure eksctl can actually use the SSO token (some environments need a refresh)
def ensure_eksctl_auth_ok():
    r = sh([EKSCTL, "get", "cluster",
            "--name", CLUSTER, "--region", REGION, "--profile", PROFILE],
           check=False, capture_output=True)
    if r.returncode == 0:
        return
    err = (r.stderr or "") + (r.stdout or "")
    if "InvalidGrantException" in err or "unable to refresh SSO token" in err:
        print("🔄 Detected expired/invalid SSO token for eksctl; refreshing...")
        # Force refresh for the profile
        sh([AWS, "sso", "logout", "--profile", PROFILE], check=False)
        sh([AWS, "sso", "login",  "--profile", PROFILE])
        # Retry once
        r2 = sh([EKSCTL, "get", "cluster",
                 "--name", CLUSTER, "--region", REGION, "--profile", PROFILE],
                check=False, capture_output=True)
        if r2.returncode != 0:
            raise RuntimeError("eksctl still cannot authenticate after SSO refresh:\n" + (r2.stderr or r2.stdout or ""))

# Call this once early in step02, after REGION/PROFILE are set:
ensure_eksctl_auth_ok()

def cluster_exists(name, region):
    info = aws_json(AWS, ["eks", "describe-cluster", "--name", name, "--region", region, "--profile", PROFILE])
    status = (info or {}).get("cluster", {}).get("status")
    return status in {"ACTIVE", "CREATING", "UPDATING"}

def wait_for_cluster_active(name, region, timeout=1800):
    start = time.time()
    while time.time() - start < timeout:
        info = aws_json(AWS, ["eks", "describe-cluster", "--name", name, "--region", region, "--profile", PROFILE])
        status = (info or {}).get("cluster", {}).get("status")
        print(f"[wait] cluster status: {status}")
        if status == "ACTIVE":
            return
        time.sleep(15)
    raise TimeoutError("EKS cluster did not reach ACTIVE in time")

def cf_stack_status(stack_name, region):
    r = sh([AWS, "cloudformation", "describe-stacks", "--region", region,
            "--stack-name", stack_name, "--output", "json", "--profile", PROFILE],
           check=False, capture_output=True)
    # If the stack truly doesn't exist, AWS returns a ValidationError.
    if r.returncode != 0:
        # Treat "does not exist" (or ValidationError) as no stack present
        return None
    try:
        return json.loads(r.stdout)["Stacks"][0]["StackStatus"]
    except Exception:
        return None

def cf_wait(stack_name, region, waiter):
    # waiter: "stack-create-complete" | "stack-delete-complete"
    sh([AWS, "cloudformation", "wait", waiter, "--region", region, "--stack-name", stack_name, "--profile", PROFILE])

def cf_wait_delete(stack_name, region):
    """
    Wait until CloudFormation stack deletion completes.
    Uses the AWS waiter first; if that fails, falls back to polling until 404.
    """
    # Primary: waiter
    try:
        sh([AWS, "cloudformation", "wait", "stack-delete-complete",
            "--region", region, "--stack-name", stack_name, "--profile", PROFILE])
        return
    except subprocess.CalledProcessError:
        pass  # fall through to manual poll

    # Fallback: poll until describe-stacks says "does not exist"
    while True:
        r = sh([AWS, "cloudformation", "describe-stacks",
                "--region", region, "--stack-name", stack_name, "--output", "json", "--profile", PROFILE],
               check=False, capture_output=True)
        if r.returncode != 0 and "does not exist" in (r.stderr or ""):
            print(f"[cf] Stack {stack_name} gone.")
            return
        time.sleep(10)

FAILED_STATUSES = {"ROLLBACK_COMPLETE", "CREATE_FAILED", "DELETE_FAILED", "ROLLBACK_FAILED"}

def ensure_cluster_with_fargate(name, region):
    stack = f"eksctl-{name}-cluster"

    # If the EKS API says it's there, skip creation
    if cluster_exists(name, region):
        print(f"✔ EKS cluster '{name}' already exists in {region}")
        return

    # If a failed/rollback stack exists, only delete it if explicitly allowed
    status = cf_stack_status(stack, region)
    if status in FAILED_STATUSES:
        msg = f"[cf] Found leftover stack {stack} in {status}."
        if not ALLOW_CFN_CLEANUP:
            raise RuntimeError(msg + " Refusing to auto-delete. Set ALLOW_CFN_CLEANUP=True to clean and retry.")
        print(msg, "Deleting...")
        sh([AWS, "cloudformation", "delete-stack", "--region", region, "--stack-name", stack, "--profile", PROFILE])
        cf_wait_delete(stack, region)

    # Create cluster
    sh([EKSCTL, "create", "cluster",
        "--name", name, "--region", region, "--with-oidc", "--fargate", "--profile", PROFILE])

    # Wait until EKS reports ACTIVE
    wait_for_cluster_active(name, region)

def fargate_profile_exists(name, region, cluster, namespace):
    try:
        r = sh([EKSCTL, "get", "fargateprofile",
                "--cluster", cluster, "--region", region, "-o", "json", "--profile", PROFILE],
               check=False, capture_output=True)
        if r.returncode != 0 or not r.stdout.strip():
            return False
        profiles = json.loads(r.stdout)
        for p in profiles:
            if p.get("name") == name:
                for sel in p.get("selectors", []):
                    if sel.get("namespace") == namespace:
                        return True
        return False
    except Exception:
        return False
    
def ensure_kubeconfig(name, region):
    if not cluster_exists(name, region):
        raise RuntimeError(f"EKS cluster '{name}' not found in {region}. Create it first.")
    sh([AWS, "eks", "update-kubeconfig", "--name", name, "--region", region, "--profile", PROFILE])
    sh([KUBECTL, "get", "ns"])  

def ensure_fargate_profile(name, region, cluster, namespace):
    if fargate_profile_exists(name, region, cluster, namespace):
        print(f"✔ Fargate profile '{name}' for namespace '{namespace}' already exists")
        return
    sh([EKSCTL, "create", "fargateprofile",
        "--cluster", cluster, "--name", name, "--namespace", namespace, "--region", region, "--profile", PROFILE])

def ensure_namespace(ns):
    r = sh([KUBECTL, "create", "namespace", ns], check=False, capture_output=True)
    # suppress noisy error if it already exists
    if r.returncode != 0 and "AlreadyExists" not in (r.stderr or ""):
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""  # disable the built-in pager
os.environ.setdefault("AWS_SDK_LOAD_CONFIG", "1")


# EKS cluster (with Fargate) & kubectl creds
# Ensure namespace exists and is scheduled on Fargate
sh([EKSCTL, "get", "cluster", "--name", CLUSTER, "--region", REGION, "--profile", PROFILE], check=False)
ensure_cluster_with_fargate(CLUSTER, REGION)
ensure_fargate_profile("batch-profile", REGION, CLUSTER, FARGATE_NS)
ensure_kubeconfig(CLUSTER, REGION)
ensure_namespace(FARGATE_NS)
print("✔ EKS (Fargate) ready and kubectl configured.")

# Create bucket if not exist
ACCOUNT_ID = subprocess.check_output([AWS,"sts","get-caller-identity","--query","Account","--output","text"], text=True).strip()
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"
print(bucket_name)
create_bucket(bucket_name, REGION, profile=PROFILE)

# ───────────────────────────────
# Create or reuse S3 RW policy
# ───────────────────────────────

os.environ.setdefault("AWS_DEFAULT_REGION", REGION)

iam = session.client("iam")
sts = session.client("sts")
account_id = sts.get_caller_identity()["Account"]

POLICY_NAME = "QELabsBatchS3RW"
policy_arn = f"arn:aws:iam::{ACCOUNT_ID}:policy/{POLICY_NAME}"

policy_doc = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow",
         "Action": ["s3:ListBucket"],
         "Resource": [f"arn:aws:s3:::{bucket_name}"]},
        {"Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
         "Resource": [f"arn:aws:s3:::{bucket_name}/*"]}
    ]
}

def ensure_policy():
    try:
        iam.get_policy(PolicyArn=policy_arn)
        print(f"✔ Policy exists: {policy_arn}")
        return policy_arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
    try:
        resp = iam.create_policy(
            PolicyName=POLICY_NAME,
            PolicyDocument=json.dumps(policy_doc),
            Description=f"S3 RW policy for {bucket_name}",
        )
        arn = resp["Policy"]["Arn"]
        print(f"✔ Created policy: {arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            resp = iam.list_policies(Scope="Local")
            for p in resp.get("Policies", []):
                if p["PolicyName"] == POLICY_NAME:
                    print(f"✔ Found existing policy: {p['Arn']}")
                    return p["Arn"]
        raise

policy_arn = ensure_policy()

# ───────────────────────────────
# Associate OIDC provider (idempotent)
# ───────────────────────────────
sh([EKSCTL, "utils", "associate-iam-oidc-provider",
    "--cluster", CLUSTER, "--region", REGION, "--approve"])

# ───────────────────────────────
# Create IRSA-linked ServiceAccount with policies
# ───────────────────────────────
ECR_READONLY = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"

sh([
    EKSCTL, "create", "iamserviceaccount",
    "--cluster", CLUSTER,
    "--namespace", FARGATE_NS,
    "--name", KSA,
    "--role-name", GSA_ROLE,
    "--attach-policy-arn", ECR_READONLY,
    "--attach-policy-arn", policy_arn,
    "--approve",
    "--region", REGION
])

# ───────────────────────────────
# Verify
# ───────────────────────────────
sh([KUBECTL, "-n", FARGATE_NS, "get", "sa", KSA, "-o", "yaml"], check=False)

print(f"✔ IRSA role '{GSA_ROLE}' bound to ServiceAccount '{KSA}' in ns '{FARGATE_NS}'")

print(f"""
===========================================
AWS Resources Summary
-------------------------------------------
Profile      : {PROFILE}
Region       : {REGION}
Account ID   : {ACCOUNT_ID}
EKS Cluster  : {CLUSTER}
ECR Repo     : {ECR_REPO}
S3 Bucket    : {bucket_name}
===========================================
""")