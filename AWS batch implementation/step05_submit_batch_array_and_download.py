import os, time, subprocess, sys
from pathlib import Path
import tomllib
import boto3
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import ensure_sso_logged_in, sh  # your helper
from botocore.exceptions import ClientError

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE       = config["AWS_profile"]["aws_profile"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
BATCH_QUEUE   = config["AWS_profile"]["BATCH_QUEUE"]
BATCH_JOB_DEF = config["AWS_profile"]["BATCH_JOB_DEF"]
shards        = config["AWS_profile"]["shards"]
data_path     = config["paths"]["data_path"]
AWS           = config["paths"]["AWS"]
LOG_GROUP = config["AWS_profile"]["LOG_GROUP"]
JOB_NAME = config["AWS_profile"]["JOB_NAME"]

ensure_sso_logged_in(AWS, PROFILE)
os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""

REGION = subprocess.check_output([AWS, "configure", "get", "region", "--profile", PROFILE], text=True).strip()
session = boto3.Session(profile_name=PROFILE, region_name=REGION)
batch   = session.client("batch")
logs    = session.client("logs")
sts     = session.client("sts")

ACCOUNT_ID = sts.get_caller_identity()["Account"]
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"

# Submit as an Array Job (size = shards). The container uses AWS_BATCH_JOB_ARRAY_INDEX (0..N-1).
job_name = JOB_NAME

submit = batch.submit_job(
    jobName=job_name,
    jobQueue=BATCH_QUEUE,
    jobDefinition=BATCH_JOB_DEF,
    arrayProperties={"size": shards},
    containerOverrides={
        # If your entrypoint needs to translate array index (0-based) to your 1-based shard folders, do it inside the container
        # (e.g., shard = int(os.environ["AWS_BATCH_JOB_ARRAY_INDEX"]) + 1).
        "environment":[]
    },
    tags={"run": str(int(time.time()))}
)

job_id = submit["jobId"]
print(f"✔ Submitted Array Job {job_name} id={job_id} size={shards}")

# statuses Batch recognizes for list_jobs
_ALL_STATUSES = ["SUBMITTED","PENDING","RUNNABLE","STARTING","RUNNING","SUCCEEDED","FAILED"]

def list_children_all_statuses(array_parent_id: str) -> list[str]:
    """Return all child job IDs for an array parent across *all* statuses."""
    ids = set()
    for st in _ALL_STATUSES:
        token = None
        while True:
            kwargs = {"arrayJobId": array_parent_id, "jobStatus": st}
            if token:
                kwargs["nextToken"] = token
            resp = batch.list_jobs(**kwargs)
            for j in resp.get("jobSummaryList", []):
                ids.add(j["jobId"])
            token = resp.get("nextToken")
            if not token:
                break
    return list(ids)

def describe_job(job_id: str) -> dict:
    return batch.describe_jobs(jobs=[job_id])["jobs"][0]

def tail_logs_until_done(parent_id: str):
    """
    Tail new lines for each child stream and exit when the array is complete.
    Exits when the *parent* job becomes SUCCEEDED/FAILED, or when statusSummary
    shows SUCCEEDED+FAILED == array size.
    """
    import time
    from botocore.exceptions import ClientError

    terminal = {"SUCCEEDED", "FAILED"}
    stream_tokens = {}          # (group, stream) -> nextForwardToken
    finished_streams = set()    # (group, stream)
    array_size = None

    while True:
        parent = describe_job(parent_id)
        p_status = parent["status"]
        arr = parent.get("arrayProperties", {}) or {}
        array_size = array_size or arr.get("size")

        # Fast exit on parent terminal
        if p_status in terminal:
            # drain any last lines one last time
            kids = list_children_all_statuses(parent_id)
            for kid in kids:
                j = describe_job(kid)
                ls = j.get("container", {}).get("logStreamName")
                if not ls:
                    continue
                key = (LOG_GROUP, ls)
                _tail_once(LOG_GROUP, ls, stream_tokens.get(key), stream_tokens, finished_streams)
            break

        # Or exit when SUCCEEDED+FAILED == size
        ss = arr.get("statusSummary", {}) or {}
        done_count = int(ss.get("SUCCEEDED", 0)) + int(ss.get("FAILED", 0))
        if array_size and done_count >= array_size:
            # optional final drain
            kids = list_children_all_statuses(parent_id)
            for kid in kids:
                j = describe_job(kid)
                ls = j.get("container", {}).get("logStreamName")
                if not ls:
                    continue
                key = (LOG_GROUP, ls)
                _tail_once(LOG_GROUP, ls, stream_tokens.get(key), stream_tokens, finished_streams)
            break

        # Otherwise continue normal tailing
        kids = list_children_all_statuses(parent_id)
        statuses = {}
        for kid in kids:
            j = describe_job(kid)
            st = j["status"]
            statuses[st] = statuses.get(st, 0) + 1

            ls = j.get("container", {}).get("logStreamName")
            if not ls:
                continue
            key = (LOG_GROUP, ls)
            _tail_once(LOG_GROUP, ls, stream_tokens.get(key), stream_tokens, finished_streams)

        # heartbeat
        summary = " ".join(f"{k}={v}" for k, v in sorted(statuses.items()))
        if summary:
            print(f"[array] {summary}")
        time.sleep(2)

def _tail_once(log_group, log_stream, prev_token, token_map, finished_set):
    """Fetch and print only new events for one stream once; update tokens."""
    from botocore.exceptions import ClientError
    key = (log_group, log_stream)
    kwargs = {"logGroupName": log_group, "logStreamName": log_stream}
    if prev_token:
        kwargs["nextToken"] = prev_token
    else:
        kwargs["startFromHead"] = True

    try:
        out = logs.get_log_events(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ResourceNotFoundException", "ThrottlingException"):
            return
        raise

    for evt in out.get("events", []):
        print(evt["message"].rstrip("\n"))

    fwd = out.get("nextForwardToken")
    if fwd:
        # if unchanged, consider stream drained for this pass
        if fwd == token_map.get(key):
            finished_set.add(key)
        token_map[key] = fwd


tail_logs_until_done(job_id)

# Final status
desc = batch.describe_jobs(jobs=[job_id])["jobs"][0]
failed = desc.get("arrayProperties", {}).get("statusSummary", {}).get("FAILED", 0)
succeeded = desc.get("arrayProperties", {}).get("statusSummary", {}).get("SUCCEEDED", 0)
print(f"Array summary -> succeeded={succeeded} failed={failed}")

# Download results S3 -> local (same as your existing script)
DEST = Path(rf"{data_path}/out"); DEST.mkdir(parents=True, exist_ok=True)
sh([AWS,"s3","sync",f"s3://{bucket_name}/output/",str(DEST)])
print(f"✔ Downloaded outputs to {DEST}")
