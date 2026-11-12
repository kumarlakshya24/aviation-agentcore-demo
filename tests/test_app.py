import json

import pytest
from fastapi.testclient import TestClient

import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(app, "DEMO_MODE", "offline")
    return TestClient(app.app)


def test_parse_chat_message_accepts_message() -> None:
    assert app.parse_chat_message(json.dumps({"type": "message", "content": "  hi  "})) == "hi"


def test_parse_chat_message_ignores_other_types() -> None:
    assert app.parse_chat_message(json.dumps({"type": "ping"})) is None
    assert app.parse_chat_message(json.dumps(["not", "a", "dict"])) is None


@pytest.mark.parametrize(
    "frame",
    [
        "not json",
        json.dumps({"type": "message"}),
        json.dumps({"type": "message", "content": "   "}),
        json.dumps({"type": "message", "content": 42}),
        json.dumps({"type": "message", "content": "x" * (app.MAX_MESSAGE_CHARS + 1)}),
    ],
)
def test_parse_chat_message_rejects_bad_frames(frame: str) -> None:
    with pytest.raises(ValueError):
        app.parse_chat_message(frame)


def test_parse_sse_handles_both_formats() -> None:
    assert app.parse_sse('{"id": 1}') == {"id": 1}
    assert app.parse_sse('event: message\ndata: {"id": 2}\n\n') == {"id": 2}


def test_mcp_url_requires_runtime_arn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app, "RUNTIME_ARN", "")
    with pytest.raises(RuntimeError):
        app._get_mcp_url()


def test_offline_metrics_and_health(client: TestClient) -> None:
    m = client.get("/api/metrics").json()
    assert m["cloudwatch_alarms"]["alarm_count"] == 3
    assert m["ec2_fleet"]["total"] == 11
    assert all(a["name"].startswith("halcyon-") for a in m["cloudwatch_alarms"]["alarms"])
    h = client.get("/api/health").json()
    assert h["status"] == "healthy" and h["mode"] == "offline"
    assert ":123456789012:" in h["runtime_arn"]


def test_offline_chat_streams_tool_call_then_text(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "What alarms are firing?"}))
        events = [ws.receive_json() for _ in range(3)]
    assert [e["type"] for e in events] == ["tool_call", "text", "done"]
    assert events[0]["name"] == "get_cloudwatch_alarms"
    traces = client.get("/api/traces").json()["traces"]
    assert traces[0]["tools_used"] == ["get_cloudwatch_alarms"]


def test_offline_scale_is_preview_until_confirmed(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text(json.dumps({"type": "message", "content": "Scale the telemetry processor to 3"}))
        preview = ws.receive_json()
        ws.receive_json(), ws.receive_json()
        ws.send_text(json.dumps({"type": "message", "content": "yes, proceed"}))
        applied = ws.receive_json()
    assert preview["input"]["confirm"] is False
    assert applied["input"]["confirm"] is True


def test_chat_reports_bad_frames(client: TestClient) -> None:
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_text("garbage")
        assert ws.receive_json()["type"] == "error"


def test_offline_unknown_question_falls_back() -> None:
    from offline import reply

    events = reply("tell me a joke")
    assert len(events) == 1 and "Offline demo mode" in events[0]["content"]
