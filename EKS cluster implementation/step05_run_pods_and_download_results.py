
import subprocess
import tempfile, textwrap, json
from pathlib import Path
import tomllib
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import *
import time

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
BUCKET_PREFIX = config["AWS_profile"]["BUCKET_PREFIX"]
FARGATE_NS = config["AWS_profile"]["FARGATE_NS"]
KSA = config["AWS_profile"]["KSA"]
ECR_REPO   = config["AWS_profile"]["ECR_REPO"]
IMAGE_TAG = config["AWS_profile"]["IMAGE_TAG"]
shards = config["AWS_profile"]["shards"]
data_path = config["paths"]["data_path"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]
# kubectl command-line tool for interacting with Kubernetes clusters (Works inside the cluster (Pods, Jobs, Deployments, Services, etc.))
KUBECTL = config["paths"]["KUBECTL"]

# SSO login
ensure_sso_logged_in(AWS, PROFILE)

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""  # disable the built-in pager

REGION = subprocess.check_output(
    [AWS, "configure", "get", "region", "--profile", PROFILE],
    text=True
).strip()

def stream_job_logs_and_progress(ns, job, total, run_id, poll_s=5):
    sel = f"job-name={job},run-id={run_id}"

    # Wait until we see pods for this run (avoid picking up old logs)
    for _ in range(120):
        r = sh([KUBECTL, "-n", ns, "get", "pods", "-l", sel, "-o", "json"],
               check=False, capture_output=True, echo=False)
        if r.returncode == 0 and r.stdout and json.loads(r.stdout).get("items"):
            break
        time.sleep(1)

    logs_cmd = [
        KUBECTL, "-n", ns, "logs",
        "-l", sel,
        "--container", "app",
        "--follow", "--prefix=true",
        "--max-log-requests", str(max(5, min(100, total))),
    ]
    print("> " + " ".join(logs_cmd))
    log_proc = subprocess.Popen(logs_cmd, text=True)

    last_len = 0
    try:
        while True:
            # Pods truth
            pr = sh([KUBECTL, "-n", ns, "get", "pods", "-l", sel, "-o", "json"],
                    check=False, capture_output=True, echo=False)

            pods_succeeded = pods_started = pods_active = pods_failed = 0
            if pr.returncode == 0 and pr.stdout:
                pj = json.loads(pr.stdout)
                for it in pj.get("items", []):
                    phase = (it.get("status", {}).get("phase") or "")
                    if phase in ("Running", "Succeeded"):
                        pods_started += 1
                    if phase == "Running":
                        pods_active += 1
                    elif phase == "Succeeded":
                        pods_succeeded += 1
                    elif phase == "Failed":
                        pods_failed += 1

            # Job status (informational only; it can be stale)
            jr = sh([KUBECTL, "-n", ns, "get", "job", job, "-o",
                     r'jsonpath={.status.succeeded}{","}{.status.active}{","}{.status.failed}'],
                    check=False, capture_output=True, echo=False)
            raw = (jr.stdout or "").strip()
            parts = (raw.split(",") + ["", "", ""])[:3]
            def toint(x): 
                try: return int(x)
                except: return 0
            succ, act, fail = map(toint, parts)

            line = (f"[job {succ}/{total}] | active={act} | failed={fail}  "
                    f"|| [pods {pods_succeeded}/{total}] | running={pods_active} | failed={pods_failed}")
            pad = " " * max(0, last_len - len(line))
            print("\r" + line + pad, end="", flush=True)
            last_len = len(line)

            # ✅ Only pods decide completion
            if (pods_succeeded >= total) and (pods_started > 0):
                print("\n✔ Job complete (by pods)")
                break

            # If the log stream dies (pod churn), restart it
            if log_proc.poll() is not None:
                log_proc = subprocess.Popen(logs_cmd, text=True)

            time.sleep(poll_s)
    finally:
        if log_proc and log_proc.poll() is None:
            try:
                os.kill(log_proc.pid, signal.SIGTERM) if os.name != "nt" else log_proc.terminate()
            except Exception:
                pass



RUN_ID = str(int(time.time()))

# ---------- 6) K8s Indexed Job on Fargate ----------
# Sanity: namespace + service account exist?
def ns_exists(ns):
    r = sh([KUBECTL, "get", "ns", ns], check=False, capture_output=True)
    return r.returncode == 0

def sa_exists(ns, sa):
    r = sh([KUBECTL, "-n", ns, "get", "sa", sa], check=False, capture_output=True)
    return r.returncode == 0

# Fargate profile already targets namespace {FARGATE_NS}, so only ensure NS exists
if not ns_exists(FARGATE_NS):
    sh([KUBECTL, "create", "namespace", FARGATE_NS])

# If you created the SA via eksctl+IRSA, it should already be present:
if not sa_exists(FARGATE_NS, KSA):
    # Minimal SA (no annotation here—eksctl created the IRSA one already; if not, use eksctl again)
    sh([KUBECTL, "-n", FARGATE_NS, "create", "sa", KSA])

# Make sure your image exists (avoid ErrImagePull later)
sh([AWS, "ecr", "describe-images",
    "--repository-name", ECR_REPO,
    "--image-ids", f"imageTag={IMAGE_TAG}",
    "--region", REGION], check=False)

ACCOUNT_ID = subprocess.check_output([AWS,"sts","get-caller-identity","--query","Account","--output","text"], text=True).strip()
bucket_name = f"{BUCKET_PREFIX}{REGION}-{ACCOUNT_ID}"
ECR_URI = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:{IMAGE_TAG}"

SHARDS, PARALLELISM = shards, shards
job_yaml = f"""
apiVersion: batch/v1
kind: Job
metadata:
  name: qelabs-sim
  namespace: {FARGATE_NS}
  labels:
    run-id: "{RUN_ID}"
spec:
  completionMode: Indexed
  completions: {SHARDS}
  parallelism: {PARALLELISM}
  ttlSecondsAfterFinished: 3600
  backoffLimit: 1
  template:
    metadata:
      labels:
        job-name: qelabs-sim
        run-id: "{RUN_ID}"
    spec:
      restartPolicy: Never
      serviceAccountName: {KSA}
      # On Fargate, just being in the profiled namespace is enough to schedule on Fargate
      containers:
      - name: app
        image: {ECR_URI}
        imagePullPolicy: IfNotPresent
        resources:
          requests:
            cpu: "1"
            memory: "2Gi"
          limits:
            cpu: "1"
            memory: "2Gi"
        env:
        - name: PYTHONUNBUFFERED
          value: "1"
        - name: MPLBACKEND
          value: "Agg"
        - name: BUCKET
          value: "{bucket_name}"
        - name: INPUT_BASE
          value: "input/"
        - name: OUTPUT_BASE
          value: "output/"
        - name: PROCESS_CAP
          value: "500"
        - name: JOB_COMPLETION_INDEX
          valueFrom:
            fieldRef:
              fieldPath: metadata.annotations['batch.kubernetes.io/job-completion-index']
"""

with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
    f.write(textwrap.dedent(job_yaml))
    jp = f.name

# Recreate job
# 1) Delete the job and wait until it's really gone
sh([KUBECTL, "-n", FARGATE_NS, "delete", "job", "qelabs-sim",
    "--ignore-not-found=true", "--wait=true"])

# 2) Delete any leftover pods (belt-and-suspenders)
sh([KUBECTL, "-n", FARGATE_NS, "delete", "pod",
    "-l", "job-name=qelabs-sim", "--ignore-not-found=true"])

# 3) Wait until no pods exist with that label
import time, json
for _ in range(60):
    r = sh([KUBECTL, "-n", FARGATE_NS, "get", "pods",
            "-l", "job-name=qelabs-sim", "-o", "json"],
           check=False, capture_output=True, echo=False)
    if r.returncode == 0 and r.stdout and json.loads(r.stdout).get("items") == []:
        break
    time.sleep(2)

sh([KUBECTL, "apply", "-f", jp])

# Optional: watch pods come up (non-fatal if you skip)
sh([KUBECTL, "-n", FARGATE_NS, "get", "pods", "-l", "job-name=qelabs-sim"])

stream_job_logs_and_progress(FARGATE_NS, "qelabs-sim", SHARDS, run_id=RUN_ID, poll_s=5)

# Wait for completion
sh([KUBECTL, "-n", FARGATE_NS, "wait", "--for=condition=complete",
    "job/qelabs-sim", "--timeout=60m"])

# Show job summary and per-shard logs
sh([KUBECTL, "-n", FARGATE_NS, "get", "job", "qelabs-sim", "-o", "wide"])

# Fetch logs for each indexed pod (pod names end with -<index>-<suffix>)
for i in range(SHARDS):
    # Find the pod for this index
    r = sh([KUBECTL, "-n", FARGATE_NS, "get", "pods",
            "-l", "job-name=qelabs-sim",
            "-o", f"jsonpath={{.items[?(@.metadata.annotations['batch\\.kubernetes\\.io/job-completion-index']=='{i}')].metadata.name}}"],
           check=False, capture_output=True)
    pod = (r.stdout or "").strip()
    if pod:
        print(f"\n=== logs for shard {i} -> {pod} ===")
        sh([KUBECTL, "-n", FARGATE_NS, "logs", pod], check=False)



# Download results from S3 to your PC

from pathlib import Path
DEST = Path(rf"{data_path}/out"); DEST.mkdir(parents=True, exist_ok=True)
# CLI sync (fast and simple)
sh([AWS,"s3","sync",f"s3://{bucket_name}/output/",str(DEST)])
print(f"✔ Downloaded outputs to {DEST}")