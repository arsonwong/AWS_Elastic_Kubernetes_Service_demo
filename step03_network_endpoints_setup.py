# Skip this if already done once

import subprocess
from pathlib import Path
import tomllib
from utilities import *
import boto3

config_path = Path("config.toml")

with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE = config["AWS_profile"]["aws_profile"]
CLUSTER = config["AWS_profile"]["CLUSTER"]

# AWS Command Line Interface (CLI)
AWS = config["paths"]["AWS"]

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
    Ensure an Interface VPC endpoint exists for e.g. 'sts' → com.amazonaws.<region>.sts.
    Returns the endpoint ID.
    """
    service_name = f"com.amazonaws.{REGION}.{service_suffix}"
    existing = find_endpoint(service_name, vpc_id)
    if existing:
        print(f"✔ Interface endpoint exists: {existing['VpcEndpointId']} ({service_name})")
        return existing["VpcEndpointId"]

    resp = ec2.create_vpc_endpoint(
        VpcId=vpc_id,
        ServiceName=service_name,
        VpcEndpointType="Interface",
        SubnetIds=subnet_ids,
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
