# Skip this if you have already uploaded the data onto the S3 bucket

import subprocess
from pathlib import Path
import tomllib
from utilities import *
from tqdm import tqdm
import os

config_path = Path("config.toml")

with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
shards = config["AWS_profile"]["shards"]
data_path = config["paths"]["data_path"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]

# SSO login
ensure_sso_logged_in(AWS, PROFILE)

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""  # disable the built-in pager

REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE],
    text=True
).strip()

ACCOUNT_ID = subprocess.check_output([AWS,"sts","get-caller-identity","--query","Account","--output","text"], text=True).strip()
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"

# uncomment to delete content of bucket first (warning - you might inadvertently delete stuff you need)
# sh([AWS, "s3", "rm", f"s3://{bucket_name}/", "--recursive"])
# Upload files
for i in tqdm(range(1, shards+1)):
    src = fr"{data_path}\{i}"
    dst = f"s3://{bucket_name}/input/{i}/"
    # Note: include/exclude order matters; exclude * then include pattern
    sh([AWS, "s3", "sync", src, dst])