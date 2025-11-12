# Skip this if already done.  Rerun if you have a new image to build and push.

import os, subprocess
from pathlib import Path
import tomllib
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import *

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

context_path = config["paths"]["context_path"]
dockerfile_path = context_path + "/Dockerfile"
PROFILE = config["AWS_profile"]["aws_profile"]
ECR_REPO = config["AWS_profile"]["ECR_REPO"]
IMAGE_TAG = config["AWS_profile"]["IMAGE_TAG"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]
# Docker Desktop for building and pushing containerized applications up to the cloud
DOCKER  = config["paths"]["DOCKER"]

# SSO login
ensure_sso_logged_in(AWS, PROFILE)

REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE],
    text=True
).strip()

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""  # disable the built-in pager

# Amazon Elastic Container Registry
# Discover account ID and ECR registry
ACCOUNT_ID = subprocess.check_output([AWS,"sts","get-caller-identity","--query","Account","--output","text"], text=True).strip()
ECR_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:{IMAGE_TAG}"

# create container repo if not already exist
if subprocess.call([AWS,"ecr","describe-repositories","--repository-names",ECR_REPO,"--region",REGION]) != 0:
    sh([AWS,"ecr","create-repository","--repository-name",ECR_REPO,"--image-scanning-configuration","scanOnPush=true","--region",REGION])

# Login docker to ECR
pwd = subprocess.check_output([AWS, "ecr", "get-login-password", "--region", REGION], text=True)
subprocess.run(
    [DOCKER, "login", "--username", "AWS", "--password-stdin", f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com"],
    input=pwd, text=True, check=True
)
# Build & push
sh([DOCKER,"build","--no-cache","-t",ECR_URI,"-f",dockerfile_path,context_path])
sh([DOCKER,"push",ECR_URI])
