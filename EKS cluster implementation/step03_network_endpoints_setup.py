# Skip this if already done once

import subprocess
from pathlib import Path
import tomllib
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import *
import boto3


directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
CLUSTER = config["AWS_profile"]["CLUSTER"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]

os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""  # disable the built-in pager

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

# --- session/clients ---
session = boto3.Session(region_name=REGION, profile_name=PROFILE)
eks = session.client("eks")
ec2 = session.client("ec2")

# --- 0) Discover VPC, subnets, and a SG in that VPC (default SG) ---
resp = eks.describe_cluster(name=CLUSTER)
vpc_id = resp["cluster"]["resourcesVpcConfig"]["vpcId"]
subnet_ids = resp["cluster"]["resourcesVpcConfig"]["subnetIds"]

# pick the default security group in the VPC (mirrors your CLI)
sg_resp = ec2.describe_security_groups(
    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "group-name", "Values": ["default"]}]
)
if not sg_resp["SecurityGroups"]:
    raise RuntimeError(f"No default security group found in VPC {vpc_id}")
sg_id = sg_resp["SecurityGroups"][0]["GroupId"]

print(f"VPC: {vpc_id}")
print(f"Subnets: {', '.join(subnet_ids)}")
print(f"Security Group: {sg_id}")

# --- helpers: ensure endpoints are present (idempotent) ---
def find_endpoint(service_name: str, vpc_id: str):
    """Return the first matching VPC endpoint dict for service in this VPC, else None."""
    pages = ec2.get_paginator("describe_vpc_endpoints").paginate(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [service_name]},
        ]
    )
    for page in pages:
        v = page.get("VpcEndpoints", [])
        if v:
            return v[0]
    return None

def ensure_interface_endpoint(service_suffix: str, vpc_id: str, subnet_ids: list[str], sg_ids: list[str], private_dns=True):
    """
    Ensure an Interface VPC endpoint exists for e.g. 'sts' → com.amazonaws.<region>.<service_suffix>.
    Picks at most one subnet per AZ (prefer private/internal-elb), to avoid DuplicateSubnetsInSameZone.
    """
    service_name = f"com.amazonaws.{REGION}.{service_suffix}"

    # If it already exists, we're done.
    existing = find_endpoint(service_name, vpc_id)
    if existing:
        print(f"✔ Interface endpoint exists: {existing['VpcEndpointId']} ({service_name})")
        return existing["VpcEndpointId"]

    # Describe subnets so we can pick one per AZ (prefer private/internal-elb)
    sub_desc = ec2.describe_subnets(SubnetIds=subnet_ids)["Subnets"]

    def is_private(sn):
        # eksctl/EKS convention: private subnets tagged internal-elb
        tags = {t["Key"]: t["Value"] for t in sn.get("Tags", [])}
        return tags.get("kubernetes.io/role/internal-elb") == "1" or tags.get("kubernetes.io/role/internal-elb") == "true"

    # Group by AZ, pick one (prefer private). If both same "privacy", pick the one with most available IPs.
    by_az = {}
    for sn in sub_desc:
        az = sn["AvailabilityZone"]
        candidate = by_az.get(az)
        if candidate is None:
            by_az[az] = sn
            continue
        def score(s):  # higher is better
            return (1 if is_private(s) else 0, s.get("AvailableIpAddressCount", 0))
        if score(sn) > score(candidate):
            by_az[az] = sn

    unique_subnets = [sn["SubnetId"] for sn in by_az.values()]
    print("Interface endpoint subnets (one per AZ):", ", ".join(unique_subnets))

    resp = ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=service_name,
        VpcEndpointType="Interface",
        SubnetIds=unique_subnets,          # ← key change
        SecurityGroupIds=sg_ids,
        PrivateDnsEnabled=private_dns,
    )
    eid = resp["VpcEndpoint"]["VpcEndpointId"]
    print(f"✔ Created interface endpoint: {eid} ({service_name})")
    return eid


def ensure_gateway_endpoint_for_s3(vpc_id: str, route_table_ids: list[str]):
    """
    Ensure an S3 Gateway endpoint exists in the VPC and is attached to the given
    route tables. If a route table already has an S3 prefix-list route, skip it
    to avoid RouteAlreadyExists.
    """
    service_name = f"com.amazonaws.{REGION}.s3"

    # 1) Resolve the S3 prefix list id for this region
    pl_resp = ec2.describe_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": [service_name]}]
    )
    if not pl_resp["PrefixLists"]:
        raise RuntimeError(f"Could not resolve S3 prefix list for {service_name}")
    s3_pl_id = pl_resp["PrefixLists"][0]["PrefixListId"]

    # 2) Filter out RTs that already have an S3 route
    def rt_has_s3_route(rt_id: str) -> bool:
        rt = ec2.describe_route_tables(RouteTableIds=[rt_id])["RouteTables"][0]
        for r in rt.get("Routes", []):
            if r.get("DestinationPrefixListId") == s3_pl_id:
                return True
        return False

    desired_rts = []
    already_routed = []
    for rt_id in route_table_ids:
        if rt_has_s3_route(rt_id):
            already_routed.append(rt_id)
        else:
            desired_rts.append(rt_id)

    # 3) Find an existing S3 gateway endpoint in this VPC (first match)
    existing = find_endpoint(service_name, vpc_id)

    if existing and existing["VpcEndpointType"] == "Gateway":
        # Add ONLY the RTs that (a) we want and (b) aren't already attached to this endpoint
        current_rts = set(existing.get("RouteTableIds", []))
        to_add = [rt for rt in desired_rts if rt not in current_rts]
        if to_add:
            try:
                ec2.modify_vpc_endpoint(
                    VpcEndpointId=existing["VpcEndpointId"],
                    AddRouteTableIds=to_add,
                )
                print(f"⟲ Updated S3 gateway endpoint {existing['VpcEndpointId']} with RTs: {', '.join(to_add)}")
            except ClientError as e:
                # If a route was created by someone else after our pre-check, ignore RouteAlreadyExists
                if e.response["Error"]["Code"] != "RouteAlreadyExists":
                    raise
        else:
            print(f"✔ S3 gateway endpoint exists: {existing['VpcEndpointId']} (no RT changes needed)")
        if already_routed:
            print(f"ℹ Skipped RTs already routed to S3: {', '.join(already_routed)}")
        return existing["VpcEndpointId"]

    # 4) No existing endpoint → create one only for RTs that actually need it
    if not desired_rts:
        # All RTs already have S3 routes; just report and return
        print(f"✔ All specified route tables already have S3 routes; no new S3 gateway endpoint created.")
        # If you want the endpoint id, you could return the one that owns any of those routes (not trivial).
        return None

    resp = ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=service_name,
        VpcEndpointType="Gateway",
        RouteTableIds=desired_rts,
    )
    eid = resp["VpcEndpoint"]["VpcEndpointId"]
    print(f"✔ Created S3 gateway endpoint: {eid} (attached RTs: {', '.join(desired_rts)})")
    if already_routed:
        print(f"ℹ Skipped RTs already routed to S3: {', '.join(already_routed)}")
    return eid


# STS Interface Endpoint (PrivateLink)
sts_endpoint_id = ensure_interface_endpoint(
    service_suffix="sts",
    vpc_id=vpc_id,
    subnet_ids=subnet_ids,
    sg_ids=[sg_id],
    private_dns=True,
)

# S3 Gateway Endpoint on non-main route tables 
rtb_resp = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
route_table_ids = []
for rt in rtb_resp["RouteTables"]:
    # main association?
    is_main = any(assoc.get("Main") for assoc in rt.get("Associations", []))
    if not is_main:
        route_table_ids.append(rt["RouteTableId"])

if not route_table_ids:
    raise RuntimeError(f"No non-main route tables found in VPC {vpc_id}; nothing to attach S3 Gateway endpoint to.")

s3_endpoint_id = ensure_gateway_endpoint_for_s3(vpc_id, route_table_ids)

print(f"Done. STS endpoint: {sts_endpoint_id}, S3 endpoint: {s3_endpoint_id}")

print("Looking up default security group for the VPC...")
resp = ec2.describe_security_groups(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
# pick one named 'default' as a fallback
default_sg = next((sg for sg in resp["SecurityGroups"] if sg["GroupName"] == "default"), None)
if default_sg:
    security_group_id = default_sg["GroupId"]
    print(f"Using default security group: {security_group_id}")
else:
    raise RuntimeError("No security group found for the VPC")

# ---------- Add ECR interface endpoints ----------
print("\n=== Creating ECR interface endpoints (api + dkr) ===")

# These allow Fargate pods in private subnets to pull images from ECR without NAT
try:
    ecr_api_endpoint_id = ensure_interface_endpoint(
        "ecr.api",
        vpc_id=vpc_id,
        subnet_ids=subnet_ids,
        sg_ids=[security_group_id],
        private_dns=True,
    )
    ecr_dkr_endpoint_id = ensure_interface_endpoint(
        "ecr.dkr",
        vpc_id=vpc_id,
        subnet_ids=subnet_ids,
        sg_ids=[security_group_id],
        private_dns=True,
    )
    print(f"✔ ECR API endpoint: {ecr_api_endpoint_id}")
    print(f"✔ ECR DKR endpoint: {ecr_dkr_endpoint_id}")
except Exception as e:
    print(f"⚠️ Failed to create ECR endpoints: {e}")
