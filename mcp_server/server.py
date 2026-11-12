"""Halcyon Flight Ops MCP server: boto3 tools for AWS infrastructure inspection.

Runs as a stateless Streamable HTTP MCP server inside a container on Amazon
Bedrock AgentCore Runtime, which expects the server on 0.0.0.0:8000/mcp.
Credentials come from the runtime's IAM execution role; region from AWS_REGION.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.config import Config
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 0.0.0.0 is the AgentCore container contract; the runtime publishes no host
# port, it fronts the container with its own SigV4-authenticated endpoint.
mcp = FastMCP(
    "halcyon-flight-ops-mcp",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    stateless_http=True,
)

REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_CLUSTER = os.environ.get("ECS_CLUSTER", "halcyon-flight-ops")
_config = Config(region_name=REGION, retries={"mode": "adaptive", "max_attempts": 5})

session = boto3.Session()
cloudwatch = session.client("cloudwatch", config=_config)
ecs_client = session.client("ecs", config=_config)
ec2_client = session.client("ec2", config=_config)
s3_client = session.client("s3", config=_config)
ce_client = session.client("ce", config=_config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Serialize a Python object to a pretty-printed JSON string.

    All datetime objects are automatically converted to ISO-8601 strings
    via the ``default=str`` fallback.

    Args:
        data: Arbitrary data structure to serialize.

    Returns:
        Pretty-printed JSON string.
    """
    return json.dumps(data, indent=2, default=str)


def _error_envelope(tool_name: str, exc: Exception) -> str:
    """Build a standardized JSON error envelope.

    Args:
        tool_name: Name of the tool that failed.
        exc: The caught exception.

    Returns:
        JSON string with ``error`` and ``tool`` keys.
    """
    return _json({"error": str(exc), "tool": tool_name})


# ---------------------------------------------------------------------------
# Tool 1 -- CloudWatch Alarms
# ---------------------------------------------------------------------------


@mcp.tool()
def get_cloudwatch_alarms() -> str:
    """Get all CloudWatch alarms currently in ALARM or INSUFFICIENT_DATA state.

    Queries the CloudWatch DescribeAlarms API twice (once per state) and
    merges the results into a single list.

    Returns:
        JSON string containing alarm_count and a list of alarm dicts with
        alarm_name, state, metric_name, namespace, threshold,
        comparison_operator, evaluation_periods, and last_updated.
    """
    try:
        alarm_resp = cloudwatch.describe_alarms(StateValue="ALARM")
        insuf_resp = cloudwatch.describe_alarms(StateValue="INSUFFICIENT_DATA")

        raw_alarms = alarm_resp.get("MetricAlarms", []) + insuf_resp.get("MetricAlarms", [])

        alarms: list[dict[str, Any]] = []
        for a in raw_alarms:
            last_updated = a.get("StateUpdatedTimestamp")
            alarms.append(
                {
                    "alarm_name": a.get("AlarmName"),
                    "state": a.get("StateValue"),
                    "metric_name": a.get("MetricName"),
                    "namespace": a.get("Namespace"),
                    "threshold": a.get("Threshold"),
                    "comparison_operator": a.get("ComparisonOperator"),
                    "evaluation_periods": a.get("EvaluationPeriods"),
                    "last_updated": (
                        last_updated.isoformat()
                        if isinstance(last_updated, datetime)
                        else str(last_updated)
                        if last_updated
                        else None
                    ),
                }
            )

        return _json({"alarm_count": len(alarms), "alarms": alarms})
    except Exception as exc:
        return _error_envelope("get_cloudwatch_alarms", exc)


# ---------------------------------------------------------------------------
# Tool 2 -- ECS Services
# ---------------------------------------------------------------------------


@mcp.tool()
def get_ecs_services(cluster: str = DEFAULT_CLUSTER) -> str:
    """List and describe all ECS services on a given cluster.

    Calls ListServices then DescribeServices to gather detailed status for
    every service on the specified cluster.

    Args:
        cluster: ECS cluster name or ARN (default: ECS_CLUSTER env var).

    Returns:
        JSON string with cluster name, service_count, and a list of service
        dicts containing service_name, status, desired_count, running_count,
        pending_count, task_definition (short name), and launch_type.
    """
    try:
        list_resp = ecs_client.list_services(cluster=cluster)
        service_arns: list[str] = list_resp.get("serviceArns", [])

        if not service_arns:
            return _json(
                {
                    "cluster": cluster,
                    "service_count": 0,
                    "services": [],
                    "message": "No services found on this cluster.",
                }
            )

        desc_resp = ecs_client.describe_services(cluster=cluster, services=service_arns)

        services: list[dict[str, Any]] = []
        for svc in desc_resp.get("services", []):
            # Extract the short task-definition family:revision from the full ARN
            task_def_arn: str = svc.get("taskDefinition", "")
            task_def_short = task_def_arn.split("/")[-1] if "/" in task_def_arn else task_def_arn

            services.append(
                {
                    "service_name": svc.get("serviceName"),
                    "status": svc.get("status"),
                    "desired_count": svc.get("desiredCount"),
                    "running_count": svc.get("runningCount"),
                    "pending_count": svc.get("pendingCount"),
                    "task_definition": task_def_short,
                    "launch_type": svc.get("launchType"),
                }
            )

        return _json(
            {
                "cluster": cluster,
                "service_count": len(services),
                "services": services,
            }
        )
    except Exception as exc:
        return _error_envelope("get_ecs_services", exc)


# ---------------------------------------------------------------------------
# Tool 3 -- EC2 Fleet
# ---------------------------------------------------------------------------


@mcp.tool()
def get_ec2_fleet(state_filter: str = "running") -> str:
    """Describe EC2 instances filtered by state, with tag extraction.

    Uses a paginator to handle accounts with many instances. Results are
    sorted alphabetically by the Name tag.

    Args:
        state_filter: Instance state to filter on (e.g. running, stopped).

    Returns:
        JSON string with state_filter, instance_count, and a list of instance
        dicts containing instance_id, instance_type, state, name, launch_time
        (ISO), private_ip, public_ip, and availability_zone.
    """
    try:
        paginator = ec2_client.get_paginator("describe_instances")
        page_iter = paginator.paginate(
            Filters=[{"Name": "instance-state-name", "Values": [state_filter]}],
        )

        instances: list[dict[str, Any]] = []
        for page in page_iter:
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
                    launch_time = inst.get("LaunchTime")
                    instances.append(
                        {
                            "instance_id": inst.get("InstanceId"),
                            "instance_type": inst.get("InstanceType"),
                            "state": inst.get("State", {}).get("Name"),
                            "name": tags.get("Name", ""),
                            "launch_time": (
                                launch_time.isoformat()
                                if isinstance(launch_time, datetime)
                                else str(launch_time)
                                if launch_time
                                else None
                            ),
                            "private_ip": inst.get("PrivateIpAddress"),
                            "public_ip": inst.get("PublicIpAddress"),
                            "availability_zone": inst.get("Placement", {}).get("AvailabilityZone"),
                        }
                    )

        # Sort alphabetically by Name tag for predictable output
        instances.sort(key=lambda i: (i["name"] or "").lower())

        return _json(
            {
                "state_filter": state_filter,
                "instance_count": len(instances),
                "instances": instances,
            }
        )
    except Exception as exc:
        return _error_envelope("get_ec2_fleet", exc)


# ---------------------------------------------------------------------------
# Tool 4 -- S3 Storage
# ---------------------------------------------------------------------------


@mcp.tool()
def get_s3_storage(bucket_prefix: str = "") -> str:
    """List S3 buckets with their storage sizes from CloudWatch metrics.

    Fetches BucketSizeBytes (StandardStorage) from the last 2 days of
    CloudWatch data for each matching bucket. Missing metrics are returned
    as 0 rather than raising an error.

    Args:
        bucket_prefix: Only include buckets whose name starts with this prefix.

    Returns:
        JSON string with bucket_count and a list of bucket dicts containing
        bucket_name, size_bytes, size_gb (rounded to 2 decimals), and
        created_date (ISO).
    """
    try:
        all_buckets = s3_client.list_buckets().get("Buckets", [])

        if bucket_prefix:
            all_buckets = [b for b in all_buckets if b["Name"].startswith(bucket_prefix)]

        now = datetime.now(UTC)
        start_time = now - timedelta(days=2)

        buckets: list[dict[str, Any]] = []
        for b in all_buckets:
            bucket_name: str = b["Name"]
            size_bytes: int = 0

            try:
                metrics = cloudwatch.get_metric_statistics(
                    Namespace="AWS/S3",
                    MetricName="BucketSizeBytes",
                    Dimensions=[
                        {"Name": "BucketName", "Value": bucket_name},
                        {"Name": "StorageType", "Value": "StandardStorage"},
                    ],
                    StartTime=start_time,
                    EndTime=now,
                    Period=86400,
                    Statistics=["Average"],
                )
                datapoints = metrics.get("Datapoints", [])
                if datapoints:
                    latest = max(datapoints, key=lambda d: d["Timestamp"])
                    size_bytes = int(latest["Average"])
            except Exception as exc:
                # Size metrics are best effort; report 0 rather than fail the listing.
                logger.warning("BucketSizeBytes lookup failed for %s: %s", bucket_name, exc)

            size_gb: float = round(size_bytes / (1024**3), 2)

            created = b.get("CreationDate")
            buckets.append(
                {
                    "bucket_name": bucket_name,
                    "size_bytes": size_bytes,
                    "size_gb": size_gb,
                    "created_date": (
                        created.isoformat() if isinstance(created, datetime) else str(created) if created else None
                    ),
                }
            )

        return _json(
            {
                "bucket_prefix": bucket_prefix or "(all)",
                "bucket_count": len(buckets),
                "buckets": buckets,
            }
        )
    except Exception as exc:
        return _error_envelope("get_s3_storage", exc)


# ---------------------------------------------------------------------------
# Tool 5 -- Cost Breakdown
# ---------------------------------------------------------------------------


@mcp.tool()
def get_cost_breakdown(days: int = 30) -> str:
    """Get AWS cost breakdown grouped by service for the last N days.

    Uses the Cost Explorer GetCostAndUsage API with MONTHLY granularity.
    Services are sorted by total cost in descending order.

    Args:
        days: Number of days to look back (default 30).

    Returns:
        JSON string with total_cost, currency, period_start, period_end,
        and a services list of {service, cost} dicts sorted descending.
    """
    try:
        now = datetime.now(UTC)
        end_date = now.strftime("%Y-%m-%d")
        start_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")

        resp = ce_client.get_cost_and_usage(
            TimePeriod={"Start": start_date, "End": end_date},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        service_costs: dict[str, float] = {}
        currency = "USD"
        grand_total = 0.0

        for result in resp.get("ResultsByTime", []):
            for group in result.get("Groups", []):
                service_name = group["Keys"][0]
                amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                currency = group["Metrics"]["UnblendedCost"]["Unit"]
                service_costs[service_name] = service_costs.get(service_name, 0.0) + amount
                grand_total += amount

        # Sort services by cost descending
        sorted_services = sorted(service_costs.items(), key=lambda x: x[1], reverse=True)
        services_list = [{"service": name, "cost": round(cost, 4)} for name, cost in sorted_services]

        return _json(
            {
                "total_cost": round(grand_total, 4),
                "currency": currency,
                "period_start": start_date,
                "period_end": end_date,
                "services": services_list,
            }
        )
    except Exception as exc:
        return _error_envelope("get_cost_breakdown", exc)


# ---------------------------------------------------------------------------
# Tool 6 -- Scale ECS Service (DANGEROUS)
# ---------------------------------------------------------------------------


@mcp.tool()
def scale_ecs_service(cluster: str, service: str, desired_count: int, confirm: bool = False) -> str:
    """Scale an ECS service to a new desired task count.

    WARNING: This is a DANGEROUS tool that modifies live infrastructure.
    It calls ecs:UpdateService to change the desired task count, which will
    immediately begin starting or stopping tasks. Use with extreme caution
    in production environments.

    Args:
        cluster: ECS cluster name or ARN.
        service: ECS service name or ARN.
        desired_count: Target number of running tasks.
        confirm: False (default) returns a preview and changes nothing.
            True applies the change; only pass it after the user agrees.

    Returns:
        JSON string with service_name, cluster, previous_desired_count,
        new_desired_count, and status. Includes a warning field when
        scaling to zero or performing a large scale-up.
    """
    try:
        # Capture current state before mutation
        desc_resp = ecs_client.describe_services(cluster=cluster, services=[service])
        current_services = desc_resp.get("services", [])

        if not current_services:
            return _json(
                {
                    "error": f"Service '{service}' not found on cluster '{cluster}'.",
                    "tool": "scale_ecs_service",
                }
            )

        previous_desired: int = current_services[0].get("desiredCount", 0)

        if desired_count < 0:
            return _json({"error": "desired_count must be >= 0", "tool": "scale_ecs_service"})

        # Two-phase guard: without confirm=True nothing is mutated.
        if not confirm:
            return _json(
                {
                    "preview": True,
                    "service_name": service,
                    "cluster": cluster,
                    "current_desired_count": previous_desired,
                    "requested_desired_count": desired_count,
                    "message": "No change made. Call again with confirm=true to apply.",
                }
            )

        # Perform the scaling operation
        update_resp = ecs_client.update_service(
            cluster=cluster,
            service=service,
            desiredCount=desired_count,
        )
        updated_svc = update_resp.get("service", {})

        # Build safety warnings for risky operations
        warning: str | None = None
        if desired_count == 0:
            warning = "Service scaled to 0 -- all tasks will be stopped."
        elif previous_desired > 0 and desired_count > previous_desired * 3:
            warning = (
                f"Large scale-up detected ({previous_desired} -> {desired_count}). "
                "Verify capacity provider and resource limits."
            )

        result: dict[str, Any] = {
            "service_name": updated_svc.get("serviceName", service),
            "cluster": cluster,
            "previous_desired_count": previous_desired,
            "new_desired_count": desired_count,
            "status": updated_svc.get("status"),
        }
        if warning:
            result["warning"] = warning

        return _json(result)
    except Exception as exc:
        return _error_envelope("scale_ecs_service", exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
