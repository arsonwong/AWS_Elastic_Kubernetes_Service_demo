import os, subprocess, time
from pathlib import Path
import tomllib
import boto3
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import ensure_sso_logged_in, sh

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE       = config["AWS_profile"]["aws_profile"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
ECR_REPO      = config["AWS_profile"]["ECR_REPO"]
BATCH_ENV     = config["AWS_profile"]["BATCH_ENV"]
BATCH_QUEUE   = config["AWS_profile"]["BATCH_QUEUE"]
BATCH_JOB_DEF = config["AWS_profile"]["BATCH_JOB_DEF"]
AWS           = config["paths"]["AWS"]

ensure_sso_logged_in(AWS, PROFILE)
os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""
REGION = subprocess.check_output([AWS, "configure", "get", "region", "--profile", PROFILE], text=True).strip()

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
batch   = session.client("batch")
sts     = session.client("sts")
iam     = session.client("iam")

ACCOUNT_ID = sts.get_caller_identity()["Account"]
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"

# 1) disable and delete job queue
try:
    batch.update_job_queue(jobQueue=BATCH_QUEUE, state="DISABLED")
    time.sleep(2)
    batch.delete_job_queue(jobQueue=BATCH_QUEUE)
    print(f"✔ Deleted Job Queue {BATCH_QUEUE}")
except Exception as e:
    print(f"(i) job queue: {e}")

# 2) delete compute environment
try:
    batch.update_compute_environment(computeEnvironment=BATCH_ENV, state="DISABLED")
    time.sleep(2)
    batch.delete_compute_environment(computeEnvironment=BATCH_ENV)
    print(f"✔ Deleted Compute Environment {BATCH_ENV}")
except Exception as e:
    print(f"(i) compute env: {e}")

# 3) de-register job definitions (all revisions)
try:
    jds = batch.describe_job_definitions(jobDefinitionName=BATCH_JOB_DEF)["jobDefinitions"]
    for jd in jds:
        batch.deregister_job_definition(jobDefinition=jd["jobDefinitionArn"])
    print(f"✔ Deregistered Job Definition {BATCH_JOB_DEF} (all revs)")
except Exception as e:
    print(f"(i) job def: {e}")

# 4) optional: delete ECR repo & S3 bucket like your original cleanup
ans = input(f"Type 'Y' to also delete S3 bucket {bucket_name} and ECR repo {ECR_REPO}: ").strip()
if ans == "Y":
    sh([AWS,"s3","rm",f"s3://{bucket_name}/","--recursive"], check=False)
    sh([AWS,"s3api","delete-bucket","--bucket",bucket_name,"--region",REGION], check=False)
    sh([AWS,"ecr","delete-repository","--repository-name",ECR_REPO,"--region",REGION,"--force"], check=False)
    print("✔ Deleted S3 bucket & ECR repo (if existed)")
