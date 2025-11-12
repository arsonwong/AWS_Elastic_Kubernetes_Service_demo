# Skip this if already done once

import os, json, time, subprocess
from pathlib import Path
import tomllib
import boto3
from botocore.exceptions import ClientError
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import ensure_sso_logged_in, create_bucket, sh  # from your utilities.py

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE       = config["AWS_profile"]["aws_profile"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
ECR_REPO      = config["AWS_profile"]["ECR_REPO"]
IMAGE_TAG     = config["AWS_profile"]["IMAGE_TAG"]
BATCH_ENV     = config["AWS_profile"]["BATCH_ENV"]
BATCH_QUEUE   = config["AWS_profile"]["BATCH_QUEUE"]
BATCH_JOB_DEF = config["AWS_profile"]["BATCH_JOB_DEF"]
LOG_GROUP = config["AWS_profile"]["LOG_GROUP"]

AWS = config["paths"]["AWS"]

# SSO login + region
ensure_sso_logged_in(AWS, PROFILE)
os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""
REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE], text=True
).strip()

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
iam   = session.client("iam")
ec2   = session.client("ec2")
batch = session.client("batch")
sts   = session.client("sts")
logs  = session.client("logs")

ACCOUNT_ID = sts.get_caller_identity()["Account"]
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"
ecr_uri = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:{IMAGE_TAG}"

# 1) S3 bucket (idempotent)
create_bucket(bucket_name, REGION, profile=PROFILE)

# 2) VPC & networking defaults (if not provided)
def default_vpc_sg_subnets():
    vpcs = ec2.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError("No default VPC found; set SUBNET_IDS/SECURITY_GROUP in config.toml.")
    vpc_id = vpcs[0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]
    # take 2+ private-ish subnets if available, else any 2+
    subnet_ids = [s["SubnetId"] for s in sorted(subnets, key=lambda x: x.get("AvailableIpAddressCount",0), reverse=True)][:3]
    # default security group
    sgs = ec2.describe_security_groups(Filters=[{"Name":"group-name","Values":["default"]},{"Name":"vpc-id","Values":[vpc_id]}])["SecurityGroups"]
    sg_id = sgs[0]["GroupId"] if sgs else None
    if not sg_id:
        raise RuntimeError("Could not find default security group.")
    return subnet_ids, sg_id

auto_subnets, auto_sg = default_vpc_sg_subnets()
SUBNET_IDS = auto_subnets
SECURITY_GROUP = auto_sg

# 3) IAM roles
#    - AWSBatchServiceRole (managed service-linked; auto when creating compute env)
#    - Execution role: ecsTaskExecutionRole (pull from ECR + logs)
#    - Job role: qelabs-batch-job-role (S3 RW for your bucket)
def ensure_role(name, assume_service, managed_policies=None, inline_policy=None):
    arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{name}"
    try:
        iam.get_role(RoleName=name)
        # make sure expected policies attached
        if managed_policies:
            attached = {p["PolicyArn"] for p in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]}
            for pol in managed_policies:
                if pol not in attached:
                    iam.attach_role_policy(RoleName=name, PolicyArn=pol)
        if inline_policy:
            iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline_policy))
        print(f"✔ Role exists: {arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    trust = {
        "Version":"2012-10-17",
        "Statement":[{"Effect":"Allow",
                      "Principal":{"Service":assume_service},
                      "Action":"sts:AssumeRole"}]
    }
    iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))
    print(f"✔ Created role: {arn}")
    if managed_policies:
        for pol in managed_policies:
            iam.attach_role_policy(RoleName=name, PolicyArn=pol)
    if inline_policy:
        iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(inline_policy))
    return arn

exec_role_arn = ensure_role(
    "ecsTaskExecutionRole",
    f"ecs-tasks.amazonaws.com",
    managed_policies=[
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy", # ECR pull + logs
    ],
)

job_role_policy = {
    "Version":"2012-10-17",
    "Statement":[
        {"Effect":"Allow","Action":["s3:ListBucket"],"Resource":[f"arn:aws:s3:::{bucket_name}"]},
        {"Effect":"Allow","Action":["s3:GetObject","s3:PutObject","s3:DeleteObject"],"Resource":[f"arn:aws:s3:::{bucket_name}/*"]},
        {"Effect":"Allow","Action":["ecr:GetAuthorizationToken","ecr:BatchCheckLayerAvailability","ecr:GetDownloadUrlForLayer","ecr:BatchGetImage"],"Resource":"*"}
]}
job_role_arn = ensure_role(
    "qelabs-batch-job-role",
    f"ecs-tasks.amazonaws.com",
    managed_policies=[],
    inline_policy=job_role_policy
)

# 4) CloudWatch Logs group
try:
    logs.create_log_group(logGroupName=LOG_GROUP)
    print(f"✔ Created log group {LOG_GROUP}")
except ClientError as e:
    if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
        raise
    print(f"✔ Log group exists: {LOG_GROUP}")

import botocore

def ensure_batch_service_linked_role(iam_client):
    """
    Make sure the service-linked role for AWS Batch exists.
    If you lack iam:CreateServiceLinkedRole, this will raise AccessDenied,
    but the CE will still work later if the role already exists.
    """
    try:
        # If it exists, this call succeeds
        iam_client.get_role(RoleName="AWSServiceRoleForBatch")
        print("✔ Service-linked role exists: AWSServiceRoleForBatch")
        return
    except iam_client.exceptions.NoSuchEntityException:
        pass

    try:
        iam_client.create_service_linked_role(AWSServiceName="batch.amazonaws.com")
        print("✔ Created service-linked role: AWSServiceRoleForBatch")
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "AccessDenied":
            print("⚠️  AccessDenied creating service-linked role; "
                  "continuing (an admin may have to create it once).")
        else:
            raise


def wait_for_ce_valid(batch_client, ce_name):
    import time
    while True:
        ce = batch_client.describe_compute_environments(
            computeEnvironments=[ce_name]
        )["computeEnvironments"][0]
        status = ce["status"]
        reason = ce.get("statusReason", "")
        state  = ce["state"]
        print(f"[wait] CE state={state} status={status} reason={reason}")
        if status == "VALID":
            print("✔ Compute environment VALID")
            return
        if status == "INVALID":
            raise RuntimeError(f"Compute environment INVALID: {reason}")
        time.sleep(5)

# Ensure the Batch service-linked role exists (preferred modern setup)
ensure_batch_service_linked_role(iam)

def recreate_ce_to_use_slr():
    # Disable → wait → delete → recreate (no serviceRole)
    try:
        batch.update_compute_environment(computeEnvironment=BATCH_ENV, state="DISABLED")
    except Exception:
        pass

    # wait until not UPDATING
    while True:
        ce = batch.describe_compute_environments(computeEnvironments=[BATCH_ENV])["computeEnvironments"][0]
        if ce["status"] != "UPDATING":
            break
        time.sleep(3)

    # If a job queue still references it, delete or detach it first
    jqs = batch.describe_job_queues().get("jobQueues", [])
    blockers = [jq["jobQueueName"] for jq in jqs
                if any(o["computeEnvironment"] in (BATCH_ENV, ce["computeEnvironmentArn"])
                       for o in jq.get("computeEnvironmentOrder", []))]
    for q in blockers:
        batch.update_job_queue(jobQueue=q, state="DISABLED")
        # wait
        while True:
            s = batch.describe_job_queues(jobQueues=[q])["jobQueues"][0]["status"]
            if s != "UPDATING": break
            time.sleep(2)
        batch.delete_job_queue(jobQueue=q)

    # delete CE
    batch.delete_compute_environment(computeEnvironment=BATCH_ENV)

    # recreate CE (no serviceRole -> uses AWSServiceRoleForBatch)
    batch.create_compute_environment(
        computeEnvironmentName=BATCH_ENV,
        type="MANAGED",
        state="ENABLED",
        computeResources={
            "type": "FARGATE",
            "maxvCpus": 30,              # match your quota
            "subnets": SUBNET_IDS,
            "securityGroupIds": [SECURITY_GROUP],
        },
    )
    wait_for_ce_valid(batch, BATCH_ENV)

def ensure_compute_env():
    resp = batch.describe_compute_environments(computeEnvironments=[BATCH_ENV])
    if resp.get("computeEnvironments"):
        ce = resp["computeEnvironments"][0]
        status = ce["status"]
        reason = ce.get("statusReason", "")
        print(f"ℹ CE found: status={status} reason={reason}")
        # If it's INVALID due to AWSBatchServiceRole, recreate to use service-linked role
        if status == "INVALID" and "AWSBatchServiceRole" in reason:
            print("↻ Recreating compute environment to use service-linked role …")
            recreate_ce_to_use_slr()
            return
        # Otherwise, just wait until VALID (handles UPDATING/ENABLED/DISABLED)
        wait_for_ce_valid(batch, BATCH_ENV)
        print(f"✔ Compute environment VALID: {BATCH_ENV}")
        return

    # CE does not exist — create fresh (no serviceRole)
    batch.create_compute_environment(
        computeEnvironmentName=BATCH_ENV,
        type="MANAGED",
        state="ENABLED",
        computeResources={
            "type": "FARGATE",
            "maxvCpus": 30,
            "subnets": SUBNET_IDS,
            "securityGroupIds": [SECURITY_GROUP],
        },
    )
    wait_for_ce_valid(batch, BATCH_ENV)
    print("✔ Compute environment CREATED and VALID")



def ensure_job_queue():
    resp = batch.describe_job_queues(jobQueues=[BATCH_QUEUE])
    exists = bool(resp.get("jobQueues"))
    if exists:
        print(f"✔ Job queue exists: {BATCH_QUEUE}")
        return
    print("Creating Job Queue")
    batch.create_job_queue(
        jobQueueName=BATCH_QUEUE,
        state="ENABLED",
        priority=1,
        computeEnvironmentOrder=[{"order":1,"computeEnvironment":BATCH_ENV}],
    )
    while True:
        jq = batch.describe_job_queues(jobQueues=[BATCH_QUEUE])["jobQueues"][0]
        if jq["status"] == "VALID": break
        time.sleep(5)
    print("✔ Job queue CREATED and VALID")

def ensure_job_def() -> str:
    # find latest ACTIVE
    resp = batch.describe_job_definitions(jobDefinitionName=BATCH_JOB_DEF, status="ACTIVE")
    if resp.get("jobDefinitions"):
        jd = sorted(resp["jobDefinitions"], key=lambda d: d["revision"])[-1]
        name_rev = f'{jd["jobDefinitionName"]}:{jd["revision"]}'
        print(f"✔ Job definition exists: {name_rev}")
        return name_rev

    jd_resp = batch.register_job_definition(
        jobDefinitionName=BATCH_JOB_DEF,
        type="container",
        platformCapabilities=["FARGATE"],   # <-- top-level, not in containerProperties
        containerProperties={
            "image": ecr_uri,
            "executionRoleArn": exec_role_arn,
            "jobRoleArn": job_role_arn,
            "resourceRequirements": [         # <-- Fargate requires this form
                {"type": "VCPU", "value": "1"},
                {"type": "MEMORY", "value": "2048"}
            ],
            "environment": [
                {"name": "BUCKET",      "value": bucket_name},
                {"name": "INPUT_BASE",  "value": "input/"},
                {"name": "OUTPUT_BASE", "value": "output/"},
                {"name": "PROCESS_CAP", "value": "1000"},
                {"name": "AWS_DEFAULT_REGION", "value": REGION},
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": "/aws/batch/qelabs",
                    "awslogs-region": REGION,
                    "awslogs-stream-prefix": "batch",
                },
            },
            "networkConfiguration": {"assignPublicIp": "DISABLED"},
            # optional, if you want to pin a Fargate platform version:
            # "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
            # map AWS_BATCH_JOB_ARRAY_INDEX -> JOB_COMPLETION_INDEX for your app
            "command": [
                "bash","-lc",
                "export JOB_COMPLETION_INDEX=$AWS_BATCH_JOB_ARRAY_INDEX; exec python /app/main.py"
            ],
        },
    )
    name_rev = f'{jd_resp["jobDefinitionName"]}:{jd_resp["revision"]}'
    print(f"✔ Registered job definition: {name_rev}")
    return name_rev


ensure_compute_env()
ensure_job_queue()
ensure_job_def()

print(f"""
===========================================
AWS Batch Ready
-------------------------------------------
Profile        : {PROFILE}
Region         : {REGION}
Account ID     : {ACCOUNT_ID}
ECR Image      : {ecr_uri}
S3 Bucket      : {bucket_name}
Compute Env    : {BATCH_ENV}
Job Queue      : {BATCH_QUEUE}
Job Definition : {BATCH_JOB_DEF}
Subnets        : {', '.join(SUBNET_IDS)}
Security Group : {SECURITY_GROUP}
Log Group      : {LOG_GROUP}
===========================================
""")
