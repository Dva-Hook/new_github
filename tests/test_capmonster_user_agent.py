from __future__ import annotations

import json
from types import SimpleNamespace

import register_ruyipage_v5 as v5


def test_fetch_capmonster_user_agent_accepts_plain_text(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "  Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0  \n"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(v5.requests, "get", lambda *args, **kwargs: Response())

    result = v5.fetch_capmonster_user_agent("https://example.test/actual")

    assert result == "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0"


def test_fetch_capmonster_user_agent_rejects_invalid_payload(monkeypatch) -> None:
    class Response:
        text = "not-a-browser"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(v5.requests, "get", lambda *args, **kwargs: Response())

    try:
        v5.fetch_capmonster_user_agent("https://example.test/actual")
    except ValueError as exc:
        assert "User-Agent" in str(exc)
    else:
        raise AssertionError("invalid User-Agent payload must be rejected")


def test_apply_capmonster_user_agent_uses_remote_value_and_reports_digest(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        v5,
        "fetch_capmonster_user_agent",
        lambda url: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0",
    )
    args = SimpleNamespace(solver="capmonster", protocol_user_agent="old-ua")

    result = v5.apply_capmonster_user_agent(
        args,
        tmp_path,
        "https://example.test/actual",
    )

    assert args.protocol_user_agent == (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0"
    )
    assert result["source"] == "capmonster-api"
    assert result["length"] == len(args.protocol_user_agent)
    assert result["sha256"]


def test_apply_capmonster_user_agent_falls_back_without_raising(
    monkeypatch, tmp_path
) -> None:
    def fail(url):
        raise TimeoutError("network timeout")

    monkeypatch.setattr(v5, "fetch_capmonster_user_agent", fail)
    args = SimpleNamespace(solver="capmonster", protocol_user_agent="configured-ua")

    result = v5.apply_capmonster_user_agent(
        args,
        tmp_path,
        "https://example.test/actual",
    )

    assert args.protocol_user_agent == "configured-ua"
    assert result == {
        "source": "configured-fallback",
        "length": len("configured-ua"),
        "sha256": v5._diagnostic_digest("configured-ua"),
        "errorType": "TimeoutError",
    }


def test_capmonster_task_uses_the_protocol_user_agent(monkeypatch, tmp_path) -> None:
    captured = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.headers = {}

        def post(self, url, **kwargs):
            captured.append((url, kwargs))
            if url.endswith("createTask"):
                return Response({"errorId": 0, "taskId": "123"})
            return Response(
                {"errorId": 0, "status": "ready", "solution": {"token": "token"}}
            )

    monkeypatch.setattr(v5.requests, "Session", Session)
    args = SimpleNamespace(
        protocol_user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/151.0.0.0",
        capmonster_proxy_mode="proxyless",
        capmonster_key="key",
        capmonster_create_url="https://api.example/createTask",
        capmonster_result_url="https://api.example/getTaskResult",
        capmonster_timeout=1,
        capmonster_poll_interval=0,
    )

    result = v5.solve_with_capmonster(
        {
            "blob": "blob-value",
            "siteKey": "site-key",
            "surl": "blizzard-api.arkoselabs.com",
            "websiteURL": "https://account.battle.net/creation/flow/creation-full",
        },
        args,
        tmp_path,
        v5.v4.ProxySettings(None, "direct"),
    )

    task_payload = captured[0][1]["json"]["task"]
    assert task_payload["userAgent"] == args.protocol_user_agent
    assert result["token"] == "token"
    assert json.loads(task_payload["data"])["blob"] == "blob-value"
