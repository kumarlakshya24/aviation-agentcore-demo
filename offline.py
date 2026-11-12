"""Offline demo mode: canned, synthetic Halcyon Aero data instead of AWS calls.

Used when DEMO_MODE=offline so the dashboard and chat work with no AWS account.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "offline.json"


@cache
def _load() -> dict[str, Any]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def health() -> dict[str, Any]:
    return dict(_load()["health"])


def metrics() -> dict[str, Any]:
    return {**_load()["metrics"], "timestamp": datetime.now(UTC).isoformat()}


def reply(text: str) -> list[dict[str, Any]]:
    """Return the chat events for a user message: first keyword match wins."""
    lowered = text.lower()
    for s in _load()["scenarios"]:
        if any(k in lowered for k in s["keywords"]):
            return [
                {
                    "type": "tool_call",
                    "name": s["tool"],
                    "input": s["input"],
                    "result": json.dumps(s["result"], indent=2),
                },
                {"type": "text", "content": s["reply"]},
            ]
    return [{"type": "text", "content": _load()["fallback"]}]
