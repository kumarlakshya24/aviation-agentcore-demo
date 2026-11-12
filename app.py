"""Halcyon Flight Ops: FastAPI backend for the AgentCore SRE agent demo.

Serves the dashboard, a WebSocket chat that runs a Bedrock Converse tool-use
loop, and live AWS metrics endpoints. Agent tool calls are routed to an MCP
server hosted on Amazon Bedrock AgentCore Runtime via SigV4-signed JSON-RPC.

    Browser <-> FastAPI (WS + REST) <-> Bedrock Converse <-> AgentCore Runtime (MCP)

With DEMO_MODE=offline (the default) nothing calls AWS: metrics, health and
chat replies come from fixtures/offline.json.

Usage:
    DEMO_MODE=offline uv run python app.py      # no AWS account needed
    DEMO_MODE=live    uv run python app.py      # needs .env with AGENT_RUNTIME_ARN
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import offline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Config: everything from the environment, nothing account-specific in code
# ---------------------------------------------------------------------------
DEMO_MODE: str = os.environ.get("DEMO_MODE", "offline").lower()
AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID: str = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5-5")
RUNTIME_ARN: str = os.environ.get("AGENT_RUNTIME_ARN", "")
ECS_CLUSTER: str = os.environ.get("ECS_CLUSTER", "halcyon-flight-ops")
HOST: str = os.environ.get("HOST", "127.0.0.1")
PORT: int = int(os.environ.get("PORT", "8080"))

MAX_MESSAGE_CHARS = 2000
MAX_AGENT_TURNS = 10
SYSTEM_PROMPT: str = (ROOT / "prompts" / "system.md").read_text(encoding="utf-8").strip()
# Adaptive retry mode backs off on throttling (429) for Bedrock and the AWS APIs.
BOTO_CONFIG = Config(region_name=AWS_REGION, retries={"mode": "adaptive", "max_attempts": 5})


def is_offline() -> bool:
    return DEMO_MODE == "offline"


# ---------------------------------------------------------------------------
# Tool definitions for Bedrock Converse: mirrors mcp_server/server.py
# ---------------------------------------------------------------------------
def _tool(name: str, description: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}


TOOL_CONFIG: dict[str, Any] = {
    "tools": [
        _tool(
            "get_cloudwatch_alarms",
            "Retrieve CloudWatch alarms in ALARM or INSUFFICIENT_DATA state.",
            {},
        ),
        _tool(
            "get_ecs_services",
            "List ECS services in a cluster with status, running count, desired count and task definition.",
            {"cluster": {"type": "string", "description": "ECS cluster name or ARN."}},
        ),
        _tool(
            "get_ec2_fleet",
            "Get EC2 instances with type, state, AZ, launch time and Name tag.",
            {"state_filter": {"type": "string", "description": "running, stopped, pending or terminated."}},
        ),
        _tool(
            "get_s3_storage",
            "List S3 buckets with sizes from CloudWatch BucketSizeBytes.",
            {"bucket_prefix": {"type": "string", "description": "Only buckets whose name starts with this."}},
        ),
        _tool(
            "get_cost_breakdown",
            "Get AWS cost by service for the last N days using Cost Explorer.",
            {"days": {"type": "integer", "description": "Days to look back. Defaults to 30."}},
        ),
        _tool(
            "scale_ecs_service",
            "Change an ECS service desired count. Call first with confirm=false to get a preview, "
            "show it to the user, and only call with confirm=true after the user explicitly agrees.",
            {
                "cluster": {"type": "string", "description": "ECS cluster name or ARN."},
                "service": {"type": "string", "description": "ECS service name or ARN."},
                "desired_count": {"type": "integer", "description": "Target number of running tasks."},
                "confirm": {"type": "boolean", "description": "false = preview only, true = apply."},
            },
            ["cluster", "service", "desired_count"],
        ),
    ]
}


# ---------------------------------------------------------------------------
# Request parsing (trust boundary: everything from the browser)
# ---------------------------------------------------------------------------
def parse_chat_message(raw: str) -> str | None:
    """Return the user text from a WebSocket frame, or None if it should be ignored.

    Expected shape: {"type": "message", "content": "<non-empty string>"}.
    Raises ValueError for malformed or oversized frames.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("frame is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("type") != "message":
        return None
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content must be a non-empty string")
    if len(content) > MAX_MESSAGE_CHARS:
        raise ValueError(f"content exceeds {MAX_MESSAGE_CHARS} characters")
    return content.strip()


# ---------------------------------------------------------------------------
# MCP client: SigV4-signed calls to AgentCore Runtime
# ---------------------------------------------------------------------------
def _get_mcp_url() -> str:
    if not RUNTIME_ARN:
        raise RuntimeError("AGENT_RUNTIME_ARN is not set (see .env.example)")
    encoded = RUNTIME_ARN.replace(":", "%3A").replace("/", "%2F")
    return f"https://bedrock-agentcore.{AWS_REGION}.amazonaws.com/runtimes/{encoded}/invocations?qualifier=DEFAULT"


def parse_sse(text: str) -> dict[str, Any]:
    """Parse an AgentCore response that is either bare JSON or an SSE stream."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return json.loads(text)


def _mcp_call(payload: dict[str, Any], session_id: str | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    url = _get_mcp_url()
    body = json.dumps(payload)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = AWSRequest(method="POST", url=url, data=body, headers=headers)
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(creds, "bedrock-agentcore", AWS_REGION).add_auth(req)
    r = httpx.post(url, content=body, headers=dict(req.headers), timeout=120)
    r.raise_for_status()
    return parse_sse(r.text), dict(r.headers)


def init_mcp_session() -> str:
    _, hdrs = _mcp_call(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "halcyon-flight-ops", "version": "1.0"},
            },
        }
    )
    return hdrs.get("mcp-session-id", "")


def call_mcp_tool(tool_name: str, tool_input: dict[str, Any], session_id: str) -> str:
    result, _ = _mcp_call(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": tool_input}},
        session_id,
    )
    content = result.get("result", {}).get("content", [])
    return content[0]["text"] if content else json.dumps(result)


# ---------------------------------------------------------------------------
# Live metrics (direct boto3, no LLM)
# ---------------------------------------------------------------------------
# Cost Explorer bills $0.01 per request and S3 size metrics update daily, so these
# two sections are cached instead of re-fetched on every 30 s dashboard refresh.
SLOW_METRIC_TTL_S = 3600
_metric_cache: dict[str, tuple[float, Any]] = {}


def fetch_live_metrics() -> dict[str, Any]:
    session = boto3.Session()

    def client(name: str) -> Any:
        return session.client(name, config=BOTO_CONFIG)

    def ecs() -> dict[str, Any]:
        c = client("ecs")
        arns = c.list_services(cluster=ECS_CLUSTER).get("serviceArns", [])
        if not arns:
            return {"active": 0, "total": 0, "services": []}
        svcs = c.describe_services(cluster=ECS_CLUSTER, services=arns)["services"]
        return {
            "active": sum(1 for s in svcs if s["status"] == "ACTIVE"),
            "total": len(svcs),
            "services": [
                {
                    "name": s["serviceName"],
                    "status": s["status"],
                    "running": s["runningCount"],
                    "desired": s["desiredCount"],
                }
                for s in svcs
            ],
        }

    def alarms() -> dict[str, Any]:
        cw = client("cloudwatch")
        found = cw.describe_alarms(StateValue="ALARM")["MetricAlarms"]
        found += cw.describe_alarms(StateValue="INSUFFICIENT_DATA")["MetricAlarms"]
        return {
            "alarm_count": len(found),
            "alarms": [
                {
                    "name": a["AlarmName"],
                    "state": a["StateValue"],
                    "description": a.get("AlarmDescription", ""),
                    "updated": a["StateUpdatedTimestamp"].isoformat() if a.get("StateUpdatedTimestamp") else None,
                }
                for a in found
            ],
        }

    def ec2() -> dict[str, Any]:
        counts: dict[str, int] = {}
        for page in client("ec2").get_paginator("describe_instances").paginate():
            for res in page.get("Reservations", []):
                for inst in res.get("Instances", []):
                    state = inst["State"]["Name"]
                    counts[state] = counts.get(state, 0) + 1
        return {"states": counts, "total": sum(counts.values())}

    def s3() -> dict[str, Any]:
        buckets = client("s3").list_buckets()["Buckets"]
        cw = client("cloudwatch")
        now = datetime.now(UTC)
        total_gb = 0.0
        for b in buckets[:20]:  # ponytail: first 20 buckets only, keeps the 30 s refresh fast
            stats = cw.get_metric_statistics(
                Namespace="AWS/S3",
                MetricName="BucketSizeBytes",
                Dimensions=[
                    {"Name": "BucketName", "Value": b["Name"]},
                    {"Name": "StorageType", "Value": "StandardStorage"},
                ],
                StartTime=now - timedelta(days=2),
                EndTime=now,
                Period=86400,
                Statistics=["Average"],
            )
            if stats["Datapoints"]:
                total_gb += max(stats["Datapoints"], key=lambda d: d["Timestamp"])["Average"] / 1024**3
        return {"total_gb": round(total_gb, 2), "bucket_count": len(buckets)}

    def cost() -> dict[str, Any]:
        now = datetime.now(UTC)
        start = now.replace(day=1).strftime("%Y-%m-%d")
        end = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        resp = client("ce").get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        by_service: dict[str, float] = {}
        for period in resp.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                by_service[g["Keys"][0]] = by_service.get(g["Keys"][0], 0.0) + float(
                    g["Metrics"]["UnblendedCost"]["Amount"]
                )
        top = sorted(by_service.items(), key=lambda kv: kv[1], reverse=True)[:5]
        return {
            "amount": f"{sum(by_service.values()):.2f}",
            "currency": "USD",
            "period": f"{start} to {end}",
            "services": [{"service": k, "cost": round(v, 2)} for k, v in top],
        }

    result: dict[str, Any] = {"timestamp": datetime.now(UTC).isoformat(), "cluster": ECS_CLUSTER}
    for key, fn in {
        "ecs_services": ecs,
        "cloudwatch_alarms": alarms,
        "ec2_fleet": ec2,
        "s3_storage": s3,
        "mtd_spend": cost,
    }.items():
        try:
            ttl = SLOW_METRIC_TTL_S if key in ("s3_storage", "mtd_spend") else 0
            cached = _metric_cache.get(key)
            if ttl and cached and time.time() - cached[0] < ttl:
                result[key] = cached[1]
            else:
                result[key] = fn()
                _metric_cache[key] = (time.time(), result[key])
        except Exception as exc:  # one failing section must not blank the dashboard
            logger.warning("%s metrics error: %s", key, exc)
            result[key] = {"error": str(exc)}
    return result


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
traces: list[dict[str, Any]] = []  # ponytail: in-memory, last 100, lost on restart

app = FastAPI(title="Halcyon Flight Ops: AgentCore SRE agent")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
async def health() -> JSONResponse:
    base = {"mode": DEMO_MODE, "model": BEDROCK_MODEL_ID, "region": AWS_REGION}
    if is_offline():
        return JSONResponse({**base, **offline.health()})
    try:
        sid = await asyncio.get_running_loop().run_in_executor(None, init_mcp_session)
        return JSONResponse({**base, "status": "healthy", "runtime_arn": RUNTIME_ARN, "mcp_session": bool(sid)})
    except Exception as exc:
        logger.exception("Health check failed")
        return JSONResponse({**base, "status": "degraded", "error": str(exc)}, status_code=503)


@app.get("/api/metrics")
async def get_metrics() -> JSONResponse:
    if is_offline():
        return JSONResponse(offline.metrics())
    return JSONResponse(await asyncio.get_running_loop().run_in_executor(None, fetch_live_metrics))


@app.get("/api/traces")
async def get_traces() -> JSONResponse:
    return JSONResponse({"traces": traces})


def _record_trace(trace: dict[str, Any], start: float) -> None:
    trace["latency_ms"] = int((time.time() - start) * 1000)
    traces.insert(0, trace)
    del traces[100:]


async def _run_live_turn(
    ws: WebSocket, bedrock: Any, messages: list[dict[str, Any]], mcp_session_id: str, trace: dict[str, Any]
) -> None:
    loop = asyncio.get_running_loop()
    for _ in range(MAX_AGENT_TURNS):
        response = await loop.run_in_executor(
            None,
            lambda: bedrock.converse(
                modelId=BEDROCK_MODEL_ID,
                system=[{"text": SYSTEM_PROMPT}],
                messages=messages,
                toolConfig=TOOL_CONFIG,
                inferenceConfig={"maxTokens": 4096, "temperature": 0.2},
            ),
        )
        usage = response.get("usage", {})
        logger.info(
            "bedrock usage model=%s input_tokens=%s output_tokens=%s",
            BEDROCK_MODEL_ID,
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
        )
        trace["tokens"] += usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

        assistant_msg = response["output"]["message"]
        messages.append(assistant_msg)

        if response.get("stopReason") != "tool_use":
            text = "\n".join(b["text"] for b in assistant_msg["content"] if "text" in b)
            await ws.send_json({"type": "text", "content": text})
            return

        tool_results: list[dict[str, Any]] = []
        for block in assistant_msg["content"]:
            if "text" in block:
                await ws.send_json({"type": "text", "content": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                result = await loop.run_in_executor(
                    None, lambda n=tu["name"], i=tu["input"]: call_mcp_tool(n, i, mcp_session_id)
                )
                await ws.send_json(
                    {"type": "tool_call", "name": tu["name"], "input": tu["input"], "result": result[:2000]}
                )
                trace["tools_used"].append(tu["name"])
                tool_results.append({"toolResult": {"toolUseId": tu["toolUseId"], "content": [{"text": result}]}})
        messages.append({"role": "user", "content": tool_results})
    await ws.send_json({"type": "text", "content": f"Stopped after {MAX_AGENT_TURNS} tool rounds."})


@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket) -> None:
    """Chat protocol: client sends {"type":"message","content":...}; server streams
    text / tool_call events and finishes each turn with {"type":"done"}."""
    await ws.accept()
    loop = asyncio.get_running_loop()
    messages: list[dict[str, Any]] = []
    bedrock: Any = None
    mcp_session_id = ""
    if not is_offline():
        bedrock = boto3.Session().client("bedrock-runtime", config=BOTO_CONFIG)
        try:
            mcp_session_id = await loop.run_in_executor(None, init_mcp_session)
        except Exception:
            logger.exception("MCP session init failed; tool calls will retry without a session id")

    try:
        while True:
            try:
                user_text = parse_chat_message(await ws.receive_text())
            except ValueError as exc:
                await ws.send_json({"type": "error", "content": str(exc)})
                continue
            if user_text is None:
                continue

            trace: dict[str, Any] = {
                "id": str(uuid.uuid4()),
                "query": user_text,
                "tools_used": [],
                "tokens": 0,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            start = time.time()
            if is_offline():
                for event in offline.reply(user_text):
                    if event["type"] == "tool_call":
                        trace["tools_used"].append(event["name"])
                    await ws.send_json(event)
            else:
                messages.append({"role": "user", "content": [{"text": user_text}]})
                await _run_live_turn(ws, bedrock, messages, mcp_session_id, trace)
            _record_trace(trace, start)
            await ws.send_json({"type": "done"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as exc:
        logger.exception("WebSocket error")
        await ws.send_json({"type": "error", "content": str(exc)})


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting in %s mode on http://%s:%s", DEMO_MODE, HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
