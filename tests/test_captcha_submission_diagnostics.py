from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import battle_protocol_flow_v4 as flow
import register_ruyipage_v5 as v5


ENTRY_URL = "https://account.battle.net/creation/flow/creation-full"
SITE_KEY = "E8A75615-1CBA-5DFF-8032-D16BCF234E10"
SURL = "blizzard-api.arkoselabs.com"


def _client(*, state_blob: str, action: str | None = None, csrf: str = "csrf"):
    form_action = action or f"{ENTRY_URL}/step/captcha-gate"
    form = flow.FormSnapshot(
        action=form_action,
        method="POST",
        source_url=ENTRY_URL,
        controls=(flow.FormControl("_csrf", csrf, kind="hidden"),),
    )
    state = SimpleNamespace(
        data={
            "status": "token-ready",
            "arkose": {
                "blob": state_blob,
                "siteKey": SITE_KEY,
                "surl": SURL,
                "websiteURL": ENTRY_URL,
            },
        }
    )
    return SimpleNamespace(entry_url=ENTRY_URL, form=form, state=state)


def _context(blob: str) -> dict[str, str]:
    return {
        "blob": blob,
        "siteKey": SITE_KEY,
        "surl": SURL,
        "websiteURL": ENTRY_URL,
    }


def test_submission_diagnosis_accepts_matching_captcha_context() -> None:
    blob = "blob-value-" + ("x" * 100)
    token = "token-value-" + ("y" * 100)

    result = v5.diagnose_captcha_submission_context(
        _client(state_blob=blob),
        _context(blob),
        token,
    )

    assert result["ok"] is True
    assert result["checks"]["state_blob_matches"] is True
    assert result["checks"]["form_step_is_captcha_gate"] is True
    assert result["blobSha256"] == hashlib.sha256(blob.encode()).hexdigest()
    assert result["tokenSha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert blob not in json.dumps(result, ensure_ascii=False)
    assert token not in json.dumps(result, ensure_ascii=False)


def test_submission_diagnosis_rejects_state_blob_mismatch() -> None:
    result = v5.diagnose_captcha_submission_context(
        _client(state_blob="old-" + ("x" * 100)),
        _context("new-" + ("x" * 100)),
        "token-" + ("y" * 100),
    )

    assert result["ok"] is False
    assert result["checks"]["state_blob_matches"] is False
    assert "state_blob_matches" in result["issues"]


def test_submission_diagnosis_rejects_non_captcha_form_or_missing_csrf() -> None:
    result = v5.diagnose_captcha_submission_context(
        _client(
            state_blob="blob-" + ("x" * 100),
            action=f"{ENTRY_URL}/step/set-battletag",
            csrf="",
        ),
        _context("blob-" + ("x" * 100)),
        "token-" + ("y" * 100),
    )

    assert result["ok"] is False
    assert result["checks"]["form_step_is_captcha_gate"] is False
    assert result["checks"]["csrf_present"] is False
    assert set(("form_step_is_captcha_gate", "csrf_present")) <= set(
        result["issues"]
    )


def test_diagnostic_trace_redacts_secret_fields(tmp_path) -> None:
    token = "token-secret"
    blob = "blob-secret"
    cookie = "cookie-secret"

    v5._diagnostic_event(
        tmp_path,
        "unit_test",
        0.0,
        token=token,
        blob=blob,
        cookie=cookie,
        proxyPassword="proxy-secret",
        tokenLength=len(token),
        tokenSha256=v5._diagnostic_digest(token),
    )

    trace = (tmp_path / "diagnostic_trace.jsonl").read_text(encoding="utf-8")
    record = json.loads(trace)
    assert record["token"] == "<redacted>"
    assert record["blob"] == "<redacted>"
    assert record["cookie"] == "<redacted>"
    assert record["proxyPassword"] == "<redacted>"
    assert record["tokenLength"] == len(token)
    assert record["tokenSha256"] == v5._diagnostic_digest(token)
    assert token not in trace
    assert blob not in trace
    assert cookie not in trace
    assert "proxy-secret" not in trace


def test_protocol_history_keeps_only_transition_metadata() -> None:
    client = SimpleNamespace(
        state=SimpleNamespace(
            data={
                "history": [
                    {
                        "at": "2026-09-01T00:00:00+00:00",
                        "completed": "provide-name",
                        "next": "provide-credentials",
                        "httpStatus": 200,
                        "responseSha256": "a" * 64,
                        "cookie": "must-not-be-exported",
                    }
                ]
            }
        )
    )

    history = v5._protocol_history(client)

    assert history == [
        {
            "transitionIndex": 1,
            "stateEventAt": "2026-09-01T00:00:00+00:00",
            "completed": "provide-name",
            "failed": "",
            "nextStep": "provide-credentials",
            "returnedStep": "",
            "classification": "",
            "httpStatus": 200,
            "responseSha256": "a" * 16,
        }
    ]


def test_server_response_signals_classify_humanity_rejection_without_text() -> None:
    result = v5._server_response_signals(
        {
            "sample": "Detecting Humanity Only humans are allowed to create accounts.",
            "errors": ["Value is invalid"],
        }
    )

    assert result["labels"] == ["humanity_only", "value_invalid"]
    assert result["errorCount"] == 1
    assert "humans are allowed" not in json.dumps(result, ensure_ascii=False)
