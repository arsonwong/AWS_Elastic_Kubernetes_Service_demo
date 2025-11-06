import os
import numpy as np
import boto3 # https://pypi.org/project/boto3/  Boto3 is the Amazon Web Services (AWS) Software Development Kit (SDK) for Python
import json

# headless plotting
os.environ.setdefault("MPLBACKEND","Agg")

bucket_name  = os.getenv("BUCKET")
input_base   = os.getenv("INPUT_BASE")  
output_base  = os.getenv("OUTPUT_BASE")
shard        = int(os.getenv("JOB_COMPLETION_INDEX"))
process_cap = int(os.getenv("PROCESS_CAP", "-1"))

input_prefix  = f"{input_base}{shard+1}/"   # e.g. "input/1/"
output_prefix = f"{output_base}{shard+1}/"   # e.g. "output/1/"

# --- S3 helpers ---
_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") 
s3 = boto3.client("s3", region_name=_region)

def s3_list_keys(prefix, suffix=None):
    """List object keys under prefix (optionally filter by suffix)."""
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            if not suffix or k.endswith(suffix):
                keys.append(k)
    return keys

def s3_download_bytes(key) -> bytes:
    return s3.get_object(Bucket=bucket_name, Key=key)["Body"].read()

def s3_put_text(key, text: str, content_type="text/plain"):
    s3.put_object(Bucket=bucket_name, Key=key, Body=text.encode("utf-8"),
                  ContentType=content_type)

def s3_put_json(key, data):
    s3.put_object(Bucket=bucket_name, Key=key, Body=json.dumps(data).encode("utf-8"),
                  ContentType="application/json")
    
# Discover inputs for this shard
json_keys = s3_list_keys(input_prefix, suffix="")

def run_one_case(s3_key: str):
    # tmp_path = download_json_to_tmp(bucket_name, s3_key)
    data = s3_download_bytes(s3_key)
    data = json.loads(data.decode("utf-8"))
    numbers = data["numbers"]
    return np.sum(np.array(numbers))

sums = []
for i, k in enumerate(json_keys):
    if process_cap>0 and i>process_cap:
        break
    if i % 10 == 0:
        print(f"Completed {i} of {len(json_keys)}")
    sums.append(run_one_case(k))
            
s3_put_text(
    f"{output_prefix}output.txt",
    "\n".join(str(x) for x in sums) + "\n"
)

print("All done")