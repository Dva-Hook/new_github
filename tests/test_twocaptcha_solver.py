from __future__ import annotations

import json
from types import SimpleNamespace

import register_ruyipage_v5 as v5


def _fake_session(captured):
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
                return Response({"errorId": 0, "taskId": "2captcha-task"})
            return Response(
                {"errorId": 0, "status": "ready", "solution": {"token": "token"}}
            )

    return Session


def test_twocaptcha_task_matches_arkose_api_contract(monkeypatch, tmp_path) -> None:
    captured = []
    monkeypatch.setattr(v5.requests, "Session", _fake_session(captured))
    args = SimpleNamespace(
        protocol_user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/151.0.0.0 Safari/537.36"
        ),
        capmonster_proxy_mode="proxyless",
        twocaptcha_key="key",
        twocaptcha_create_url="https://api.2captcha.com/createTask",
        twocaptcha_result_url="https://api.2captcha.com/getTaskResult",
        twocaptcha_timeout=1,
        twocaptcha_poll_interval=0,
    )

    result = v5.solve_with_twocaptcha(
        {
            "blob": "blob-value",
            "siteKey": "site-key",
            "surl": "https://blizzard-api.arkoselabs.com",
            "websiteURL": "https://account.battle.net/creation/flow/creation-full",
        },
        args,
        tmp_path,
        v5.v4.ProxySettings(None, "direct"),
    )

    task_payload = captured[0][1]["json"]["task"]
    assert task_payload["type"] == "FunCaptchaTaskProxyless"
    assert task_payload["websitePublicKey"] == "site-key"
    assert task_payload["funcaptchaApiJSSubdomain"] == "blizzard-api.arkoselabs.com"
    assert task_payload["userAgent"] == args.protocol_user_agent
    assert json.loads(task_payload["data"])["blob"] == "blob-value"
    assert result["provider"] == "2captcha"
    assert result["token"] == "token"


def test_twocaptcha_uses_selected_proxy_fields(monkeypatch, tmp_path) -> None:
    captured = []
    monkeypatch.setattr(v5.requests, "Session", _fake_session(captured))
    args = SimpleNamespace(
        protocol_user_agent="Mozilla/5.0 test",
        capmonster_proxy_mode="proxy",
        twocaptcha_key="key",
        twocaptcha_create_url="https://api.2captcha.com/createTask",
        twocaptcha_result_url="https://api.2captcha.com/getTaskResult",
        twocaptcha_timeout=1,
        twocaptcha_poll_interval=0,
    )
    proxy = v5.v4.ProxySettings(
        "http://user:pass@127.0.0.1:8080", "proxy", "http", "127.0.0.1", 8080, True
    )

    result = v5.solve_with_twocaptcha(
        {"siteKey": "site-key", "websiteURL": "https://example.test"},
        args,
        tmp_path,
        proxy,
    )

    task_payload = captured[0][1]["json"]["task"]
    assert task_payload["type"] == "FunCaptchaTask"
    assert task_payload["proxyType"] == "http"
    assert task_payload["proxyAddress"] == "127.0.0.1"
    assert task_payload["proxyPort"] == 8080
    assert task_payload["proxyLogin"] == "user"
    assert task_payload["proxyPassword"] == "pass"
    assert result["proxyMode"] == "proxy"
