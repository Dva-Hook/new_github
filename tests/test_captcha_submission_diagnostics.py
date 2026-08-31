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
