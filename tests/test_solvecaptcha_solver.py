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

        @property
        def text(self):
            return json.dumps(self._payload)

    class Session:
        def __init__(self):
            self.headers = {}

        def post(self, url, **kwargs):
            captured.append((url, kwargs))
            if url.endswith("/in.php"):
                return Response({"status": 1, "request": "solve-id"})
            return Response({"status": 1, "request": "solve-token"})

    return Session


def _args(**overrides):
    values = {
        "protocol_user_agent": "Mozilla/5.0 test",
        "solvecaptcha_key": "key",
        "solvecaptcha_create_url": "https://api.solvecaptcha.com/in.php",
        "solvecaptcha_result_url": "https://api.solvecaptcha.com/res.php",
        "solvecaptcha_timeout": 1,
        "solvecaptcha_poll_interval": 0,
        "capmonster_proxy_mode": "proxyless",
        "entry_url": "https://account.battle.net/creation/flow/creation-full",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_solvecaptcha_fun_captcha_contract(monkeypatch, tmp_path) -> None:
    captured = []
    monkeypatch.setattr(v5.requests, "Session", _fake_session(captured))

    result = v5.solve_with_solvecaptcha(
        {
            "blob": "blob-value",
            "siteKey": "site-key",
            "surl": "https://blizzard-api.arkoselabs.com",
            "websiteURL": "https://account.battle.net/creation/flow/creation-full",
        },
        _args(),
        tmp_path,
        v5.v4.ProxySettings(None, "direct"),
    )

    create_url, create_kwargs = captured[0]
    assert create_url.endswith("/in.php")
    assert create_kwargs["data"]["method"] == "funcaptcha"
    assert create_kwargs["data"]["publickey"] == "site-key"
    assert create_kwargs["data"]["surl"] == "https://blizzard-api.arkoselabs.com"
    assert create_kwargs["data"]["userAgent"] == "Mozilla/5.0 test"
    assert json.loads(create_kwargs["data"]["data"])["blob"] == "blob-value"
    assert result["provider"] == "solvecaptcha"
    assert result["token"] == "solve-token"


def test_solvecaptcha_uses_selected_proxy(monkeypatch, tmp_path) -> None:
    captured = []
    monkeypatch.setattr(v5.requests, "Session", _fake_session(captured))
    proxy = v5.v4.ProxySettings(
        "http://user:pass@127.0.0.1:8080",
        "proxy",
        "http",
        "127.0.0.1",
        8080,
        True,
    )

    v5.solve_with_solvecaptcha(
        {"siteKey": "site-key", "websiteURL": "https://example.test"},
        _args(capmonster_proxy_mode="proxy"),
        tmp_path,
        proxy,
    )

    payload = captured[0][1]["data"]
    assert payload["proxytype"] == "HTTP"
    assert payload["proxy"] == "user:pass@127.0.0.1:8080"
