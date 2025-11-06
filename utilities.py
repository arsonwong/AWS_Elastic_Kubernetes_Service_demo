import subprocess
import json

import boto3
from botocore.exceptions import ClientError

def create_bucket(bucket, region, profile=None):
    # Use a Session so we can honor your profile
    session = boto3.Session(profile_name=profile, region_name=region)
    s3 = session.client("s3")

    # Fast idempotency: if you can head the bucket, you're done
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"✔ Bucket '{bucket}' already exists (and you can access it).")
        return
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchBucket", "404 Not Found"):
            # 403/Forbidden means it exists but not yours; don't try to create
            if code in ("403", "Forbidden"):
                print(f"ℹ Bucket '{bucket}' exists but you don't own it; choose another name.")
                return
            # other errors bubble up
            pass

    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        print(f"✔ Created bucket '{bucket}' in region '{region}'.")
    except ClientError as e:
        err = e.response.get("Error", {}).get("Code", "")
        if err == "BucketAlreadyOwnedByYou":
            print(f"✔ Bucket '{bucket}' already owned by you.")
        elif err == "BucketAlreadyExists":
            print(f"❌ Bucket name '{bucket}' is taken globally. Pick another.")
        else:
            raise

# Example
# create_bucket("my-unique-bucket-12345", "us-west-2", profile="your-profile")


def ensure_sso_logged_in(AWS, PROFILE):
    """Runs aws sso login only if the SSO session is expired or missing."""
    try:
        # Try a simple command that requires valid credentials
        subprocess.run(
            [AWS, "sts", "get-caller-identity", "--profile", PROFILE],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        print(f"✅ Already logged in to AWS SSO profile: {PROFILE}")
    except subprocess.CalledProcessError:
        print(f"🔄 SSO session expired or missing, logging in to {PROFILE}...")
        subprocess.run([AWS, "sso", "login", "--profile", PROFILE], check=True)
        print("✅ AWS SSO login successful!")

def sh(cmd, check=True, echo=True, **kwargs):
    if echo:
        print(">", " ".join(cmd))
    return subprocess.run(cmd, text=True, check=check, **kwargs)

def aws_json(AWS, args, check=False):
    """
    Call AWS CLI and parse JSON. Returns dict or None.
    """
    r = sh([AWS, *args, "--output", "json"], check=check, capture_output=True)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout) if r.stdout else None
    except json.JSONDecodeError:
        return None