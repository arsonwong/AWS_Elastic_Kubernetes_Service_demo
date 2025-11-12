import os, subprocess, time
from pathlib import Path
import tomllib
import boto3
from botocore.exceptions import ClientError
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utilities import ensure_sso_logged_in

directory = os.path.dirname(os.path.abspath(__file__))
config_path = Path(os.path.join(directory,"config.toml"))
with open(config_path, "rb") as f:
    config = tomllib.load(f)

PROFILE        = config["AWS_profile"]["aws_profile"]
SUBNET_IDS     = config["AWS_profile"].get("SUBNET_IDS", [])
SECURITY_GROUP = config["AWS_profile"].get("SECURITY_GROUP", "")
AWS            = config["paths"]["AWS"]

ensure_sso_logged_in(AWS, PROFILE)
os.environ["AWS_PROFILE"] = PROFILE
os.environ["AWS_PAGER"] = ""
REGION = subprocess.check_output([AWS, "configure", "get", "region", "--profile", PROFILE], text=True).strip()

session = boto3.Session(profile_name=PROFILE, region_name=REGION)
ec2 = session.client("ec2")

# ---- helpers ----
def get_default_vpc():
    vpcs = ec2.describe_vpcs(Filters=[{"Name":"isDefault","Values":["true"]}])["Vpcs"]
    return vpcs[0]["VpcId"]

def resolve_vpc_and_subnets():
    if SUBNET_IDS:
        subs = ec2.describe_subnets(SubnetIds=SUBNET_IDS)["Subnets"]
        vpc_ids = {s["VpcId"] for s in subs}
        if len(vpc_ids) != 1:
            raise RuntimeError("SUBNET_IDS must belong to the same VPC.")
        return list(vpc_ids)[0], [s["SubnetId"] for s in subs]
    # fallback: default VPC + pick a few subnets
    vpc_id = get_default_vpc()
    subs = ec2.describe_subnets(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["Subnets"]
    subs.sort(key=lambda s: s.get("AvailableIpAddressCount",0), reverse=True)
    picked = []
    seen_az = set()
    for s in subs:
        az = s.get("AvailabilityZone")
        if az not in seen_az or len(picked) < 2:
            picked.append(s["SubnetId"])
            seen_az.add(az)
        if len(picked) >= 3:
            break
    if len(picked) < 2:
        raise RuntimeError("Need at least 2 subnets; specify SUBNET_IDS in config.toml.")
    return vpc_id, picked

def get_route_tables_for_subnets(vpc_id, subnet_ids):
    # map subnet -> route table (use explicit associations first, else the main RT)
    rtbs = ec2.describe_route_tables(Filters=[{"Name":"vpc-id","Values":[vpc_id]}])["RouteTables"]
    by_subnet = {}
    main_rtb = None
    for rtb in rtbs:
        assoc = rtb.get("Associations", [])
        for a in assoc:
            if a.get("Main"):
                main_rtb = rtb["RouteTableId"]
            sid = a.get("SubnetId")
            if sid:
                by_subnet[sid] = rtb["RouteTableId"]
    rtb_ids = set(by_subnet.get(sid, main_rtb) for sid in subnet_ids)
    return [r for r in rtb_ids if r]

def ensure_interface_endpoint(vpc_id, service, subnets, sg_id):
    svc_name = f"com.amazonaws.{REGION}.{service}"
    res = ec2.describe_vpc_endpoints(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "service-name", "Values": [svc_name]},
        {"Name": "vpc-endpoint-type", "Values": ["Interface"]},
    ])["VpcEndpoints"]

    if res:
        vpe = res[0]
        want_subs = set(subnets)
        have_subs = set(vpe.get("SubnetIds", []))
        have_sg_ids = {g["GroupId"] for g in vpe.get("Groups", [])}
        want_sg_ids = {sg_id} if sg_id else set()

        add_subs = list(want_subs - have_subs)
        add_sgs  = list(want_sg_ids - have_sg_ids)

        if add_subs or add_sgs:
            kwargs = {"VpcEndpointId": vpe["VpcEndpointId"]}
            if add_subs:
                kwargs["AddSubnetIds"] = add_subs
            if add_sgs:
                kwargs["AddSecurityGroupIds"] = add_sgs
            ec2.modify_vpc_endpoint(**kwargs)
            print(f"✔ Updated interface endpoint {service} (added: "
                  f"{'subs ' + ','.join(add_subs) if add_subs else ''} "
                  f"{'sgs ' + ','.join(add_sgs) if add_sgs else ''})")
        else:
            print(f"✔ Interface endpoint exists: {service}")
        return vpe["VpcEndpointId"]

    resp = ec2.create_vpc_endpoint(
        VpcEndpointType="Interface",
        VpcId=vpc_id,
        ServiceName=svc_name,
        SubnetIds=subnets,
        SecurityGroupIds=[sg_id] if sg_id else [],
        PrivateDnsEnabled=True,
    )
    vpe_id = resp["VpcEndpoint"]["VpcEndpointId"]
    print(f"✔ Created interface endpoint {service}: {vpe_id}")
    return vpe_id




def ensure_gateway_endpoint(vpc_id, service, route_table_ids):
    svc_name = f"com.amazonaws.{REGION}.{service}"
    res = ec2.describe_vpc_endpoints(Filters=[
        {"Name":"vpc-id","Values":[vpc_id]},
        {"Name":"service-name","Values":[svc_name]},
        {"Name":"vpc-endpoint-type","Values":["Gateway"]}
    ])["VpcEndpoints"]

    if res:
        vpe = res[0]
        # For Gateway endpoints, RouteTableIds is already list[str]
        have_rts = set(vpe.get("RouteTableIds", []))
        want_rts = set(route_table_ids)
        missing = list(want_rts - have_rts)
        if missing:
            ec2.modify_vpc_endpoint(
                VpcEndpointId=vpe["VpcEndpointId"],
                AddRouteTableIds=missing
            )
            print(f"✔ Updated gateway endpoint {service} with RTs: {missing}")
        else:
            print(f"✔ Gateway endpoint exists: {service}")
        return vpe["VpcEndpointId"]

    resp = ec2.create_vpc_endpoint(
        VpcEndpointType="Gateway",
        VpcId=vpc_id,
        ServiceName=svc_name,
        RouteTableIds=route_table_ids
    )
    vpe_id = resp["VpcEndpoint"]["VpcEndpointId"]
    print(f"✔ Created gateway endpoint {service}: {vpe_id}")
    return vpe_id


# ---- run ----
vpc_id, subnet_ids = resolve_vpc_and_subnets()
print(f"VPC: {vpc_id}")
print(f"Subnets: {', '.join(subnet_ids)}")

# If you don’t have a dedicated SG for endpoints, create or set SECURITY_GROUP in config.
if not SECURITY_GROUP:
    # fall back to default SG in that VPC
    sgs = ec2.describe_security_groups(Filters=[{"Name":"group-name","Values":["default"]},{"Name":"vpc-id","Values":[vpc_id]}])["SecurityGroups"]
    if not sgs:
        raise RuntimeError("No SECURITY_GROUP provided and default SG not found in VPC.")
    SECURITY_GROUP = sgs[0]["GroupId"]

# Required endpoints for private Batch on Fargate:
# - S3 (Gateway) for S3 data
# - ECR (Interface: api + dkr) to pull images
# - STS (Interface) for auth
# - Logs (Interface) for CloudWatch Logs
rtb_ids = get_route_tables_for_subnets(vpc_id, subnet_ids)
ensure_gateway_endpoint(vpc_id, "s3", rtb_ids)
ensure_interface_endpoint(vpc_id, "ecr.api", subnet_ids, SECURITY_GROUP)
ensure_interface_endpoint(vpc_id, "ecr.dkr", subnet_ids, SECURITY_GROUP)
ensure_interface_endpoint(vpc_id, "sts", subnet_ids, SECURITY_GROUP)
ensure_interface_endpoint(vpc_id, "logs", subnet_ids, SECURITY_GROUP)

print("✅ Private endpoints ready for Batch on Fargate.")
