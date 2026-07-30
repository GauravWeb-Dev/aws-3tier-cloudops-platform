"""
reaper_lambda.py
AWS-native cost-safety backstop for aws-3tier-cloudops-platform.

Runs hourly via EventBridge. Finds resources tagged Environment=Demo whose
DemoSessionStart tag is older than SESSION_MAX_HOURS, and force-deletes the
cost-bearing ones directly via AWS APIs -- independent of GitHub Actions,
independent of `terraform destroy` succeeding.

Design decisions (see MASTER_PROMPT_V13.md Section 2 for rationale):
  - Does NOT touch terraform.tfstate. State reconciliation happens via
    `terraform plan -refresh-only` at the start of the next pipeline run.
  - Deployed as PERMANENT infra (see reaper_infra.tf), tagged Environment=Permanent,
    in a SEPARATE Terraform state from the demo stack it watches. This Lambda
    must never be discoverable by its own sweep.
  - Deletion order matters: ALB listeners/target groups before the ALB,
    ECS services scaled to 0 before deletion, ASG capacity zeroed before
    instance termination, RDS last-storage things before the instance.
    Dependency errors (e.g. DependencyViolation) are caught and logged,
    not raised -- the next hourly run retries.

Required IAM permissions (attach via reaper_infra.tf, NOT the bootstrap policy):
  elasticloadbalancing:Describe*, elasticloadbalancing:DeleteListener,
  elasticloadbalancing:DeleteLoadBalancer, elasticloadbalancing:DeleteTargetGroup,
  ecs:ListServices, ecs:UpdateService, ecs:DeleteService, ecs:DescribeServices,
  autoscaling:UpdateAutoScalingGroup, autoscaling:DescribeAutoScalingGroups,
  rds:DescribeDBInstances, rds:DeleteDBInstance,
  ec2:DescribeVpcEndpoints, ec2:DeleteVpcEndpoints,
  wafv2:GetWebACL, wafv2:DeleteWebACL, wafv2:DisassociateWebACL,
  tag:GetResources, cloudwatch:PutMetricData
"""

import boto3
import datetime
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SESSION_MAX_HOURS = int(os.environ.get("SESSION_MAX_HOURS", "2"))
DEMO_TAG_KEY = "Environment"
DEMO_TAG_VALUE = "Demo"
SESSION_START_TAG = "DemoSessionStart"  # ISO8601, set by the pipeline at `terraform apply` time

tagging = boto3.client("resourcegroupstaggingapi")
elbv2 = boto3.client("elbv2")
ecs = boto3.client("ecs")
autoscaling = boto3.client("autoscaling")
rds = boto3.client("rds")
ec2 = boto3.client("ec2")
wafv2 = boto3.client("wafv2")
cloudwatch = boto3.client("cloudwatch")


def _session_expired(tags: dict) -> bool:
    start_str = tags.get(SESSION_START_TAG)
    if not start_str:
        # No session-start marker means we can't safely judge age -- skip,
        # don't guess. Alert instead of destroying blind.
        logger.warning("Resource missing %s tag, skipping (cannot verify age).", SESSION_START_TAG)
        return False
    try:
        started = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable %s value: %s", SESSION_START_TAG, start_str)
        return False
    age = datetime.datetime.now(datetime.timezone.utc) - started
    return age > datetime.timedelta(hours=SESSION_MAX_HOURS)


def _get_demo_resources():
    """Returns list of (arn, tag_dict) for everything tagged Environment=Demo."""
    resources = []
    paginator = tagging.get_paginator("get_resources")
    for page in paginator.paginate(
        TagFilters=[{"Key": DEMO_TAG_KEY, "Values": [DEMO_TAG_VALUE]}]
    ):
        for r in page.get("ResourceTagMappingList", []):
            tag_dict = {t["Key"]: t["Value"] for t in r.get("Tags", [])}
            resources.append((r["ResourceARN"], tag_dict))
    return resources


def _kill_albs(arns):
    for arn in arns:
        if ":loadbalancer/" not in arn:
            continue
        try:
            listeners = elbv2.describe_listeners(LoadBalancerArn=arn)["Listeners"]
            for l in listeners:
                elbv2.delete_listener(ListenerArn=l["ListenerArn"])
            elbv2.delete_load_balancer(LoadBalancerArn=arn)
            logger.info("Deleted ALB %s", arn)
        except Exception as e:
            logger.warning("ALB delete deferred for %s: %s", arn, e)

    # Target groups only after their ALBs are gone
    try:
        tgs = elbv2.describe_target_groups()["TargetGroups"]
        for tg in tgs:
            tg_tags = tagging.get_resources(
                ResourceARNList=[tg["TargetGroupArn"]]
            ).get("ResourceTagMappingList", [])
            if tg_tags and {t["Key"]: t["Value"] for t in tg_tags[0]["Tags"]}.get(DEMO_TAG_KEY) == DEMO_TAG_VALUE:
                elbv2.delete_target_group(TargetGroupArn=tg["TargetGroupArn"])
                logger.info("Deleted target group %s", tg["TargetGroupArn"])
    except Exception as e:
        logger.warning("Target group cleanup deferred: %s", e)


def _kill_ecs(arns):
    for arn in arns:
        if ":service/" not in arn:
            continue
        try:
            cluster_arn = arn.split(":service/")[1].split("/")[0]
            service_name = arn.split("/")[-1]
            ecs.update_service(cluster=cluster_arn, service=service_name, desiredCount=0)
            ecs.delete_service(cluster=cluster_arn, service=service_name, force=True)
            logger.info("Deleted ECS service %s", arn)
        except Exception as e:
            logger.warning("ECS service delete deferred for %s: %s", arn, e)


def _kill_asg(arns):
    for arn in arns:
        if ":autoScalingGroup" not in arn:
            continue
        try:
            asg_name = arn.split("/")[-1]
            autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name, MinSize=0, MaxSize=0, DesiredCapacity=0
            )
            logger.info("Scaled ASG %s to zero (deletion follows once instances drain)", asg_name)
        except Exception as e:
            logger.warning("ASG scale-down deferred for %s: %s", asg_name, e)


def _kill_rds(arns):
    for arn in arns:
        if ":db:" not in arn:
            continue
        try:
            db_id = arn.split(":db:")[-1]
            rds.delete_db_instance(
                DBInstanceIdentifier=db_id,
                SkipFinalSnapshot=True,
                DeleteAutomatedBackups=True,
            )
            logger.info("Deleted RDS instance %s", db_id)
        except Exception as e:
            logger.warning("RDS delete deferred for %s: %s", db_id, e)


def _kill_vpc_endpoints(arns):
    endpoint_ids = [a.split("/")[-1] for a in arns if ":vpc-endpoint/" in a]
    if endpoint_ids:
        try:
            ec2.delete_vpc_endpoints(VpcEndpointIds=endpoint_ids)
            logger.info("Deleted VPC endpoints %s", endpoint_ids)
        except Exception as e:
            logger.warning("VPC endpoint delete deferred: %s", e)


def _kill_waf(arns):
    for arn in arns:
        if ":webacl/" not in arn:
            continue
        try:
            name = arn.split("/")[-2]
            web_acl_id = arn.split("/")[-1]
            lock_token = wafv2.get_web_acl(Name=name, Scope="REGIONAL", Id=web_acl_id)["LockToken"]
            wafv2.delete_web_acl(Name=name, Scope="REGIONAL", Id=web_acl_id, LockToken=lock_token)
            logger.info("Deleted WAF Web ACL %s", name)
        except Exception as e:
            logger.warning("WAF delete deferred for %s: %s", arn, e)


def handler(event, context):
    resources = _get_demo_resources()
    expired_arns = [arn for arn, tags in resources if _session_expired(tags)]

    if not expired_arns:
        logger.info("No expired demo resources found. %d demo resources currently within session window.", len(resources))
        return {"expired_count": 0, "total_demo_resources": len(resources)}

    logger.info("Reaping %d expired resources.", len(expired_arns))

    # Order matters: edge -> compute -> data -> network glue
    _kill_albs(expired_arns)
    _kill_waf(expired_arns)
    _kill_ecs(expired_arns)
    _kill_asg(expired_arns)
    _kill_rds(expired_arns)
    _kill_vpc_endpoints(expired_arns)

    cloudwatch.put_metric_data(
        Namespace="DemoReaper",
        MetricData=[{"MetricName": "ResourcesReaped", "Value": len(expired_arns), "Unit": "Count"}],
    )

    return {"expired_count": len(expired_arns), "total_demo_resources": len(resources)}
