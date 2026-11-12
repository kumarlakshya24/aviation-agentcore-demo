import json
from typing import Any

import pytest
import server


class FakeECS:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def describe_services(self, cluster: str, services: list[str]) -> dict[str, Any]:
        return {"services": [{"serviceName": services[0], "desiredCount": 2, "status": "ACTIVE"}]}

    def update_service(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"service": {"serviceName": kwargs["service"], "status": "ACTIVE"}}


@pytest.fixture
def ecs(monkeypatch: pytest.MonkeyPatch) -> FakeECS:
    fake = FakeECS()
    monkeypatch.setattr(server, "ecs_client", fake)
    return fake


def test_scale_without_confirm_changes_nothing(ecs: FakeECS) -> None:
    out = json.loads(server.scale_ecs_service("c", "svc", 3))
    assert out["preview"] is True and out["current_desired_count"] == 2
    assert ecs.updates == []


def test_scale_with_confirm_applies(ecs: FakeECS) -> None:
    out = json.loads(server.scale_ecs_service("c", "svc", 3, confirm=True))
    assert out["new_desired_count"] == 3
    assert ecs.updates == [{"cluster": "c", "service": "svc", "desiredCount": 3}]


def test_scale_rejects_negative(ecs: FakeECS) -> None:
    out = json.loads(server.scale_ecs_service("c", "svc", -1, confirm=True))
    assert "error" in out and ecs.updates == []
