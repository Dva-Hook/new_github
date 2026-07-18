# -*- coding: utf-8 -*-
"""Persistent HTTP form-flow primitives used by the hybrid registration runner."""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import re
import secrets
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup


FLOW_PATH = "/creation/flow/creation-full"
FLOW_STEPS = (
    "initial-tou-agreement",
    "row-redirect-to-tassadar",
    "login",
    "get-started",
    "provide-name",
    "provide-credentials",
    "legal-and-opt-ins",
    "set-password",
    "set-battletag",
    "captcha-gate",
)
NEXT_STEP = {
    "login": "get-started",
    "initial-tou-agreement": "get-started",
    "row-redirect-to-tassadar": "login",
    "get-started": "provide-name",
    "provide-name": "provide-credentials",
    "provide-credentials": "legal-and-opt-ins",
    "legal-and-opt-ins": "set-password",
    "set-password": "set-battletag",
    "set-battletag": "captcha-gate",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class FormControl:
    name: str
    value: str = ""
    kind: str = "text"
    checked: bool = False
    disabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "kind": self.kind,
            "checked": self.checked,
            "disabled": self.disabled,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FormControl":
        return cls(
            name=str(value.get("name") or ""),
            value=str(value.get("value") or ""),
            kind=str(value.get("kind") or "text"),
            checked=bool(value.get("checked")),
            disabled=bool(value.get("disabled")),
        )


@dataclass(frozen=True)
class FormSnapshot:
    action: str
    method: str
    source_url: str
    controls: tuple[FormControl, ...] = field(default_factory=tuple)

    @property
    def step(self) -> str:
        return step_from_action(self.action)

    @property
    def csrf(self) -> str:
        values = self.values("_csrf")
        return values[-1] if values else ""

    def values(self, name: str) -> list[str]:
        return [item.value for item in self.controls if item.name == name and not item.disabled]

    def hidden_fields(self) -> list[tuple[str, str]]:
        return [
            (item.name, item.value)
            for item in self.controls
            if item.kind == "hidden" and not item.disabled
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "method": self.method,
            "sourceUrl": self.source_url,
            "step": self.step,
            "csrf": self.csrf,
            "controls": [item.to_dict() for item in self.controls],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FormSnapshot":
        return cls(
            action=str(value.get("action") or ""),
            method=str(value.get("method") or "POST").upper(),
            source_url=str(value.get("sourceUrl") or ""),
            controls=tuple(
                FormControl.from_dict(item)
                for item in (value.get("controls") or [])
                if isinstance(item, Mapping)
            ),
        )


def step_from_action(action: str) -> str:
    path = urlsplit(str(action or "")).path.rstrip("/")
    if "/login" in path.lower():
        return "login"
    match = re.search(r"/step/([^/?#]+)$", path, flags=re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if path.endswith(FLOW_PATH):
        return "creation-root"
    return "unknown"


def _form_score(form: Any, preferred_control: str = "") -> int:
    action = str(form.get("action") or "")
    score = 0
    if preferred_control and any(
        str(element.get("name") or "") == preferred_control
        for element in form.find_all(["input", "select", "textarea"])
    ):
        score += 100
    if str(form.get("id") or "") == "flow-form":
        score += 20
    if FLOW_PATH in action:
        score += 12
    if form.select_one('input[name="_csrf"]'):
        score += 8
    if form.select_one('input[name="accountName"]'):
        score += 6
    if "/login" in action.lower():
        score += 4
    return score


def parse_flow_form(
    html: str,
    base_url: str,
    *,
    preferred_control: str = "",
) -> FormSnapshot:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    forms = list(soup.find_all("form"))
    if not forms:
        raise ValueError("response does not contain a form")
    form = max(forms, key=lambda item: _form_score(item, preferred_control))
    controls: list[FormControl] = []
    for element in form.find_all(["input", "select", "textarea"]):
        name = str(element.get("name") or "")
        if not name:
            continue
        disabled = element.has_attr("disabled")
        tag = str(element.name or "").lower()
        if tag == "select":
            options = list(element.find_all("option"))
            selected = [item for item in options if item.has_attr("selected")]
            if not selected and options:
                selected = [options[0]]
            for option in selected or [None]:
                value = "" if option is None else str(option.get("value", option.get_text()))
                controls.append(FormControl(name, value, "select", True, disabled))
            continue
        if tag == "textarea":
            controls.append(FormControl(name, element.get_text(), "textarea", True, disabled))
            continue
        controls.append(
            FormControl(
                name=name,
                value=str(element.get("value") or ""),
                kind=str(element.get("type") or "text").lower(),
                checked=element.has_attr("checked"),
                disabled=disabled,
            )
        )
    return FormSnapshot(
        action=urljoin(base_url, str(form.get("action") or base_url)),
        method=str(form.get("method") or "POST").upper(),
        source_url=str(base_url),
        controls=tuple(controls),
    )


def _without_names(fields: Iterable[tuple[str, str]], names: set[str]) -> list[tuple[str, str]]:
    return [(name, value) for name, value in fields if name not in names]


def _identity(identity: Mapping[str, Any], key: str) -> str:
    return str(identity.get(key) or "")


def build_step_fields(
    form: FormSnapshot,
    step: str,
    identity: Mapping[str, Any],
    *,
    country: str = "GBR",
    opt_in: bool = False,
    arkose_token: str = "",
) -> list[tuple[str, str]]:
    step = str(step).lower()
    hidden = form.hidden_fields()
    if step == "login":
        return _without_names(hidden, {"accountName"}) + [("accountName", _identity(identity, "email"))]
    if step == "get-started":
        names = {"country", "dob-day", "dob-format", "dob-month", "dob-plain", "dob-year", "webdriver"}
        dob_format = (form.values("dob-format") or ["DMY"])[-1] or "DMY"
        return _without_names(hidden, names) + [
            ("country", str(country).upper()),
            ("dob-day", _identity(identity, "birth_day").zfill(2)),
            ("dob-format", dob_format),
            ("dob-month", _identity(identity, "birth_month").zfill(2)),
            ("dob-plain", ""),
            ("dob-year", _identity(identity, "birth_year")),
        ]
    if step == "provide-name":
        full_name = _identity(identity, "full_name").strip()
        if not full_name:
            full_name = " ".join(
                item for item in (
                    _identity(identity, "first_name").strip(),
                    _identity(identity, "last_name").strip(),
                ) if item
            )
        control_names = {item.name for item in form.controls if not item.disabled}
        first_name_field = next(
            (name for name in ("first-name", "firstName", "given-name", "givenName") if name in control_names),
            "",
        )
        last_name_field = next(
            (name for name in ("last-name", "lastName", "family-name", "familyName") if name in control_names),
            "",
        )
        if first_name_field and last_name_field:
            return _without_names(hidden, {first_name_field, last_name_field, "full-name"}) + [
                (first_name_field, _identity(identity, "first_name")),
                (last_name_field, _identity(identity, "last_name")),
            ]
        return _without_names(hidden, {"full-name"}) + [("full-name", full_name)]
    if step == "provide-credentials":
        return _without_names(hidden, {"email", "phone-number"}) + [
            ("email", _identity(identity, "email")),
            ("phone-number", _identity(identity, "phone_number")),
        ]
    if step == "legal-and-opt-ins":
        fields = list(hidden)
        for control in form.controls:
            if control.disabled or control.kind not in {"checkbox", "radio"}:
                continue
            include = control.name == "tou-agreements-implicit" or (
                opt_in and control.name == "opt-in-blizzard-news-special-offers"
            )
            item = (control.name, control.value or "true")
            if include and item not in fields:
                fields.append(item)
        return fields
    names_and_values = {
        "set-password": ("password", _identity(identity, "password")),
        "set-battletag": ("battletag", _identity(identity, "battle_tag")),
        "captcha-gate": ("arkose", str(arkose_token)),
    }
    if step in names_and_values:
        name, value = names_and_values[step]
        return _without_names(hidden, {name}) + [(name, value)]
    if step == "row-redirect-to-tassadar":
        return list(hidden)
    raise ValueError(f"unsupported flow step: {step}")


def build_country_probe_fields(form: FormSnapshot, country: str) -> list[tuple[str, str]]:
    names = {"country", "dob-day", "dob-format", "dob-month", "dob-plain", "dob-year", "webdriver"}
    dob_format = (form.values("dob-format") or ["DMY"])[-1] or "DMY"
    return _without_names(form.hidden_fields(), names) + [
        ("country", str(country).upper()),
        ("dob-day", ""),
        ("dob-format", dob_format),
        ("dob-month", ""),
        ("dob-plain", ""),
        ("dob-year", ""),
        ("webdriver", "false"),
    ]


def build_initial_tou_fields(
    form: FormSnapshot,
    country: str,
    *,
    accept: bool,
) -> list[tuple[str, str]]:
    fields = _without_names(form.hidden_fields(), {"country", "document-select"})
    fields.append(("country", str(country).upper()))
    document = form.values("document-select")
    if document:
        fields.append(("document-select", document[-1]))
    if accept:
        for control in form.controls:
            item = (control.name, control.value or "true")
            if (
                not control.disabled
                and control.kind in {"checkbox", "radio"}
                and control.name == "tou-agreements-explicit"
                and item not in fields
            ):
                fields.append(item)
    return fields


def encode_multipart(
    fields: Sequence[tuple[str, Any]],
    *,
    boundary: Optional[str] = None,
) -> tuple[bytes, str]:
    if boundary is None:
        alphabet = string.ascii_letters + string.digits
        boundary = "----WebKitFormBoundary" + "".join(secrets.choice(alphabet) for _ in range(16))
    chunks: list[bytes] = []
    for raw_name, raw_value in fields:
        name = str(raw_name).replace("\\", "\\\\").replace('"', '\\"')
        value = "" if raw_value is None else str(raw_value)
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def validate_transition(current_step: str, next_step: str, *, country_probe: bool = False) -> None:
    current_step = str(current_step).lower()
    next_step = str(next_step).lower()
    if country_probe and current_step == "get-started" and next_step == "get-started":
        return
    if current_step == "initial-tou-agreement":
        if next_step == "row-redirect-to-tassadar":
            return
        if country_probe and next_step in {
            "initial-tou-agreement",
            "login",
            "get-started",
        }:
            return
        if not country_probe and next_step in {"get-started", "login"}:
            return
    if current_step == "row-redirect-to-tassadar" and next_step in {
        "initial-tou-agreement",
        "login",
        "get-started",
    }:
        return
    expected = NEXT_STEP.get(current_step)
    if expected != next_step:
        raise RuntimeError(
            f"flow transition rejected after {current_step}: expected {expected or 'completion'}, got {next_step}"
        )


def _first_valid_blob(candidates: Iterable[tuple[str, str]]) -> tuple[str, str]:
    for source, raw in candidates:
        candidate = html_lib.unescape(unquote(str(raw or ""))).strip().replace("\\/", "/")
        if len(candidate) < 80 or candidate.lower().startswith(("blob:", "http://", "https://")):
            continue
        return candidate, source
    return "", ""


def detect_arkose_context(html: str, website_url: str) -> dict[str, Any]:
    text = html_lib.unescape(str(html or ""))
    candidates: list[tuple[str, str]] = []
    patterns = (
        ("html:data-arkose-exchange-data", r"data-arkose-exchange-data\s*=\s*[\"']([^\"']{80,10000})[\"']"),
        ("html:json-blob", r"[\"']blob[\"']\s*:\s*[\"']([^\"']{80,10000})[\"']"),
        ("html:data-blob", r"data\[blob\]\s*[=:]\s*[\"']?([^&\"'\s<]{80,10000})"),
        ("html:query-blob", r"(?:[?&]|\\u0026)blob=([^&\"'\s<]{80,10000})"),
    )
    for label, pattern in patterns:
        candidates.extend((label, match.group(1)) for match in re.finditer(pattern, text, re.I))
    blob, source = _first_valid_blob(candidates)

    site_key = ""
    for pattern in (
        r"[\"'](?:publicKey|public_key|siteKey|pkey)[\"']\s*:\s*[\"']([0-9a-f-]{36})[\"']",
        r"/v\d+/([0-9a-f-]{36})/api\.js",
        r"/fc/gt2/public_key/([0-9a-f-]{36})",
        r"data-pkey=[\"']([0-9a-f-]{36})[\"']",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            site_key = match.group(1).upper()
            break

    surl = ""
    for pattern in (
        r"[\"']surl[\"']\s*:\s*[\"'](https?://[^\"']+)[\"']",
        r"//([a-z0-9.-]*arkoselabs\.com)/v\d+/",
        r"(https://[a-z0-9.-]*arkoselabs\.com)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            surl = match.group(1).replace("\\/", "/").rstrip("/")
            break
    return {
        "blob": blob,
        "blobLength": len(blob),
        "siteKey": site_key,
        "surl": surl,
        "websiteURL": str(website_url),
        "source": source or "none",
    }


def classify_registration_response(html: str, expected_email: str = "") -> dict[str, Any]:
    soup = BeautifulSoup(str(html or ""), "html.parser")
    text = " ".join(soup.get_text(" ", strip=True).split())
    lowered = text.lower()
    expected = str(expected_email or "").strip().lower()
    errors: list[str] = []
    for element in soup.select('[role="alert"], .error, [class*="error"], [data-testid*="error"]'):
        value = " ".join(element.get_text(" ", strip=True).split())
        if value and value not in errors:
            errors.append(value)
    step_meta = soup.select_one("#step-meta-data, [data-step-id]")
    step_id = str(step_meta.get("data-step-id") or "") if step_meta else ""
    step_has_errors = (
        str(step_meta.get("data-step-has-errors") or "").strip().lower()
        if step_meta
        else ""
    )
    has_create_success_meta = (
        step_id == "create-success" and step_has_errors not in {"true", "1"}
    )
    player_meta = soup.select_one("#player-id[data-player-account-id]")
    player_account_id = (
        str(player_meta.get("data-player-account-id") or "") if player_meta else ""
    )
    account_email_element = soup.select_one(
        ".step__banner--account-identifier, [data-account-email]"
    )
    account_email = (
        " ".join(account_email_element.get_text(" ", strip=True).split())
        if account_email_element
        else ""
    )
    has_all_set = bool(
        re.search(r"you(?:'|\u2019)?re\s+all\s+set|\ball\s+set\b", text, re.I)
    )
    has_created = bool(re.search(r"account\s+has\s+been\s+created|has\s+been\s+created", text, re.I))
    has_download = "download battle.net app" in lowered
    has_icon = bool(soup.select_one('#success-icon, [data-testid*="success"], [class*="success"] svg'))
    has_email = expected in lowered if expected else True
    success = bool(
        has_email
        and (
            has_create_success_meta
            or (has_all_set and (has_created or has_download))
            or (has_icon and has_created)
        )
    )
    rejected = any(
        re.search(r"invalid|incorrect|try again|not quite right|failed|失败|无效", item, re.I)
        for item in errors
    )
    if success:
        status = "success"
    elif rejected:
        status = "rejected"
    else:
        try:
            parse_flow_form(str(html or ""), "https://HOST")
            status = "flow"
        except ValueError:
            status = "unknown"
    return {
        "status": status,
        "success": success,
        "hasSuccessIcon": has_icon,
        "hasAllSet": has_all_set,
        "hasCreated": has_created,
        "hasDownloadApp": has_download,
        "hasExpectedEmail": has_email,
        "stepId": step_id,
        "playerAccountId": player_account_id,
        "accountEmail": account_email,
        "errors": errors,
        "sample": text[:400],
    }


def _same_site(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "Strict" if text == "strict" else "None" if text == "none" else "Lax"


def serialize_cookie_jar(cookie_jar: Iterable[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for cookie in cookie_jar:
        rest = getattr(cookie, "_rest", {}) or {}
        record: dict[str, Any] = {
            "name": str(getattr(cookie, "name", "")),
            "value": str(getattr(cookie, "value", "")),
            "domain": str(getattr(cookie, "domain", "") or ""),
            "path": str(getattr(cookie, "path", "/") or "/"),
            "secure": bool(getattr(cookie, "secure", False)),
            "httpOnly": "HttpOnly" in rest or "httponly" in rest,
            "sameSite": _same_site(rest.get("SameSite") or rest.get("samesite")),
        }
        if getattr(cookie, "expires", None) is not None:
            record["expires"] = int(cookie.expires)
        result.append(record)
    return result


def playwright_cookies_from_records(
    records: Sequence[Mapping[str, Any]],
    fallback_url: str,
) -> list[dict[str, Any]]:
    parsed = urlsplit(fallback_url)
    origin = f"{parsed.scheme or 'https'}://{parsed.netloc}"
    result: list[dict[str, Any]] = []
    for item in records:
        cookie: dict[str, Any] = {
            "name": str(item.get("name") or ""),
            "value": str(item.get("value") or ""),
            "secure": bool(item.get("secure")),
            "httpOnly": bool(item.get("httpOnly")),
            "sameSite": _same_site(item.get("sameSite")),
        }
        domain = str(item.get("domain") or "")
        if domain:
            cookie.update(domain=domain, path=str(item.get("path") or "/"))
        else:
            cookie["url"] = origin
        if item.get("expires") not in (None, "", 0, -1):
            cookie["expires"] = float(item["expires"])
        result.append(cookie)
    return result


def merge_playwright_cookies(cookie_store: Any, records: Sequence[Mapping[str, Any]]) -> None:
    for item in records:
        name = str(item.get("name") or "")
        if name:
            cookie_store.set(
                name,
                str(item.get("value") or ""),
                domain=str(item.get("domain") or ""),
                path=str(item.get("path") or "/"),
                secure=bool(item.get("secure")),
            )


class PersistentFlowState:
    def __init__(self, path: Path, data: MutableMapping[str, Any]) -> None:
        self.path = Path(path)
        self.data = data

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        identity: Mapping[str, Any],
        profile: Optional[Mapping[str, Any]] = None,
    ) -> "PersistentFlowState":
        now = _now()
        state = cls(Path(path), {
            "schemaVersion": 1,
            "createdAt": now,
            "updatedAt": now,
            "status": "new",
            "identity": dict(identity),
            "profile": dict(profile or {}),
            "cookies": [],
            "form": None,
            "response": None,
            "arkose": {},
            "result": None,
            "history": [],
        })
        state.save()
        return state

    @classmethod
    def load(cls, path: Path) -> "PersistentFlowState":
        target = Path(path)
        data = json.loads(target.read_text(encoding="utf-8"))
        if int(data.get("schemaVersion") or 0) != 1:
            raise ValueError(f"unsupported state schema: {data.get('schemaVersion')!r}")
        return cls(target, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updatedAt"] = _now()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def checkpoint(
        self,
        status: str,
        *,
        event: Optional[Mapping[str, Any]] = None,
        **updates: Any,
    ) -> None:
        self.data["status"] = str(status)
        self.data.update(updates)
        if event is not None:
            self.data.setdefault("history", []).append({"at": _now(), **dict(event)})
        self.save()


class BattleProtocolClient:
    """Chrome-impersonating, resumable client for the server-rendered flow."""

    def __init__(
        self,
        state: PersistentFlowState,
        output_dir: Path,
        *,
        entry_url: str,
        proxy: Optional[str] = None,
        impersonate: str = "chrome",
        user_agent: Optional[str] = None,
        accept_language: str = "en-US,en;q=0.9",
        timeout: float = 60.0,
        session: Any = None,
    ) -> None:
        if session is None:
            from curl_cffi import requests as curl_requests

            session = curl_requests.Session(impersonate=impersonate)
        self.session = session
        self.state = state
        self.output_dir = Path(output_dir)
        self.entry_url = str(entry_url)
        self.proxy = str(proxy or "") or None
        self.timeout = float(timeout)
        self._response_index = len(state.data.get("history") or [])
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": str(accept_language),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if user_agent:
            headers["User-Agent"] = str(user_agent)
        self.session.headers.update(headers)
        merge_playwright_cookies(self.session.cookies, state.data.get("cookies") or [])

    @property
    def form(self) -> Optional[FormSnapshot]:
        raw = self.state.data.get("form")
        form = FormSnapshot.from_dict(raw) if isinstance(raw, Mapping) else None
        if form and form.step in FLOW_STEPS and FLOW_PATH in urlsplit(form.action).path:
            canonical = self.entry_url.rstrip("/") + "/step/" + form.step
            if form.action != canonical:
                form = FormSnapshot(canonical, form.method, form.source_url, form.controls)
        return form

    def cookie_records(self) -> list[dict[str, Any]]:
        return serialize_cookie_jar(self.session.cookies.jar)

    def playwright_cookies(self) -> list[dict[str, Any]]:
        return playwright_cookies_from_records(self.cookie_records(), self.entry_url)

    def recover_arkose_from_last_response(self) -> dict[str, Any]:
        current = dict(self.state.data.get("arkose") or {})
        if current.get("blob"):
            return current
        response = self.state.data.get("response") or {}
        path = Path(str(response.get("path") or ""))
        if not path.is_file():
            return current
        recovered = detect_arkose_context(path.read_text(encoding="utf-8"), self.entry_url)
        if recovered.get("blob"):
            self.state.checkpoint(
                str(self.state.data.get("status") or "captcha-gate"),
                arkose=recovered,
                event={
                    "completed": "arkose-context-recovery",
                    "source": recovered.get("source"),
                    "blobLength": recovered.get("blobLength"),
                },
            )
            return recovered
        return current

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        if self.proxy and "proxy" not in kwargs and "proxies" not in kwargs:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        return self.session.request(method.upper(), url, **kwargs)

    def _save_response(self, label: str, response: Any) -> dict[str, Any]:
        self._response_index += 1
        raw = bytes(response.content or b"")
        digest = hashlib.sha256(raw).hexdigest()
        safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(label)).strip("_") or "response"
        path = self.output_dir / "protocol_responses" / f"{self._response_index:02d}_{safe}_{digest[:12]}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {
            "label": str(label),
            "statusCode": int(response.status_code),
            "url": str(response.url),
            "length": len(raw),
            "sha256": digest,
            "path": str(path.resolve()),
            "contentType": str(response.headers.get("content-type") or ""),
        }

    def _checkpoint_response(
        self,
        status: str,
        form: FormSnapshot,
        response_meta: Mapping[str, Any],
        *,
        event: Mapping[str, Any],
        arkose: Optional[Mapping[str, Any]] = None,
    ) -> None:
        updates: dict[str, Any] = {
            "form": form.to_dict(),
            "cookies": self.cookie_records(),
            "response": dict(response_meta),
            "error": None,
        }
        if arkose is not None:
            updates["arkose"] = dict(arkose)
        self.state.checkpoint(status, event=event, **updates)

    def bootstrap(self) -> FormSnapshot:
        existing = self.form
        if existing and self.state.data.get("status") not in {"new", "bootstrap"}:
            return existing
        response = self._request("GET", self.entry_url)
        meta = self._save_response("bootstrap_get", response)
        if not 200 <= int(response.status_code) < 400:
            raise RuntimeError(f"bootstrap GET failed: HTTP {response.status_code}")
        form = parse_flow_form(response.text, str(response.url))
        if form.step == "login":
            fields = build_step_fields(form, "login", self.state.data["identity"])
            response = self._request(
                form.method or "POST",
                form.action,
                data=fields,
                headers={"Referer": form.source_url},
            )
            meta = self._save_response("login_submit", response)
            if not 200 <= int(response.status_code) < 400:
                raise RuntimeError(f"login/account-name submit failed: HTTP {response.status_code}")
            form = parse_flow_form(response.text, str(response.url))
        if form.step not in {"initial-tou-agreement", "get-started"}:
            outcome = classify_registration_response(
                response.text,
                self.state.data["identity"].get("email", ""),
            )
            raise RuntimeError(
                f"bootstrap ended on unexpected form {form.step!r}: {outcome.get('sample') or 'no diagnostic text'}"
            )
        self._checkpoint_response(
            form.step,
            form,
            meta,
            event={"completed": "bootstrap", "next": form.step},
        )
        return form

    def _fragment_request(
        self,
        method: str,
        form: FormSnapshot,
        fields: Sequence[tuple[str, Any]],
    ) -> Any:
        body, content_type = encode_multipart(fields)
        parsed = urlsplit(self.entry_url)
        return self._request(
            method,
            form.action,
            data=body,
            headers={
                "Accept": "text/html, */*; q=0.01",
                "Content-Type": content_type,
                "Origin": f"{parsed.scheme}://{parsed.netloc}",
                "Referer": self.entry_url,
                "X-Flow-Fragment": "true",
            },
        )

    def _accept_fragment(
        self,
        current_step: str,
        response: Any,
        *,
        country_probe: bool = False,
    ) -> FormSnapshot:
        label = f"{current_step}_{'probe' if country_probe else 'submit'}"
        meta = self._save_response(label, response)
        if not 200 <= int(response.status_code) < 300:
            self.state.checkpoint(
                current_step,
                response=meta,
                cookies=self.cookie_records(),
                error=f"HTTP {response.status_code}",
                event={"failed": current_step, "httpStatus": int(response.status_code)},
            )
            raise RuntimeError(f"{current_step} failed: HTTP {response.status_code}")
        try:
            next_form = parse_flow_form(
                response.text,
                self.entry_url,
                preferred_control=(
                    "tou-agreements-explicit"
                    if country_probe or self.state.data.get("countryProbed")
                    else ""
                ),
            )
        except ValueError as exc:
            outcome = classify_registration_response(
                response.text,
                self.state.data["identity"].get("email", ""),
            )
            self.state.checkpoint(
                current_step,
                response=meta,
                cookies=self.cookie_records(),
                result=outcome,
                event={"failed": current_step, "reason": "missing-next-form"},
            )
            raise RuntimeError(
                f"{current_step} response has no next form: {outcome.get('sample') or exc}"
            ) from exc
        try:
            validate_transition(current_step, next_form.step, country_probe=country_probe)
        except RuntimeError:
            outcome = classify_registration_response(
                response.text,
                self.state.data["identity"].get("email", ""),
            )
            self.state.checkpoint(
                current_step,
                form=next_form.to_dict(),
                response=meta,
                cookies=self.cookie_records(),
                result=outcome,
                event={"failed": current_step, "returned": next_form.step},
            )
            raise
        arkose = detect_arkose_context(response.text, self.entry_url) if current_step == "set-battletag" else None
        self._checkpoint_response(
            next_form.step,
            next_form,
            meta,
            event={
                "completed": "country-probe" if country_probe else current_step,
                "next": next_form.step,
                "httpStatus": int(response.status_code),
                "responseSha256": meta["sha256"],
            },
            arkose=arkose,
        )
        return next_form

    def submit_country_probe(self, country: str) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != "get-started":
            raise RuntimeError(f"country probe requires get-started, current={form.step}")
        response = self._fragment_request("PUT", form, build_country_probe_fields(form, country))
        result = self._accept_fragment("get-started", response, country_probe=True)
        self.state.data["countryProbed"] = True
        self.state.save()
        return result

    def submit_initial_country_probe(self, country: str) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != "initial-tou-agreement":
            raise RuntimeError(f"initial country probe requires initial-tou-agreement, current={form.step}")
        response = self._fragment_request(
            "PUT",
            form,
            build_initial_tou_fields(form, country, accept=False),
        )
        result = self._accept_fragment("initial-tou-agreement", response, country_probe=True)
        self.state.data["countryProbed"] = True
        self.state.save()
        return result

    def submit_initial_tou(self, country: str) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != "initial-tou-agreement":
            raise RuntimeError(f"initial agreement requires initial-tou-agreement, current={form.step}")
        response = self._fragment_request(
            "POST",
            form,
            build_initial_tou_fields(form, country, accept=True),
        )
        return self._accept_fragment("initial-tou-agreement", response)

    def submit_login(self) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != "login":
            raise RuntimeError(f"login submit requires login, current={form.step}")
        fields = build_step_fields(form, "login", self.state.data["identity"])
        response = self._request(
            form.method or "POST",
            form.action,
            data=fields,
            headers={"Referer": form.source_url},
        )
        meta = self._save_response("login_submit", response)
        if not 200 <= int(response.status_code) < 400:
            raise RuntimeError(f"login/account-name submit failed: HTTP {response.status_code}")
        next_form = parse_flow_form(response.text, str(response.url))
        validate_transition("login", next_form.step)
        self._checkpoint_response(
            next_form.step,
            next_form,
            meta,
            event={"completed": "login", "next": next_form.step, "httpStatus": int(response.status_code)},
        )
        return next_form

    def submit_passthrough(self, step: str) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != step:
            raise RuntimeError(f"passthrough submit requires {step}, current={form.step}")
        fields = build_step_fields(form, step, self.state.data["identity"])
        return self._accept_fragment(step, self._fragment_request("POST", form, fields))

    def submit_step(self, step: str, *, country: str, opt_in: bool = False) -> FormSnapshot:
        form = self.form or self.bootstrap()
        if form.step != step:
            raise RuntimeError(f"refusing to replay {step}: current server form is {form.step}")
        fields = build_step_fields(
            form,
            step,
            self.state.data["identity"],
            country=country,
            opt_in=opt_in,
        )
        return self._accept_fragment(step, self._fragment_request("POST", form, fields))

    def run_to_captcha(
        self,
        *,
        country: str,
        opt_in: bool = False,
        country_probe: bool = True,
    ) -> FormSnapshot:
        form = self.form or self.bootstrap()
        transitions = 0
        while form.step != "captcha-gate":
            transitions += 1
            if transitions > 32:
                raise RuntimeError(
                    f"registration flow exceeded transition limit at {form.step}"
                )
            if form.step not in FLOW_STEPS:
                raise RuntimeError(f"unsupported current flow form: {form.step}")
            if form.step == "login":
                form = self.submit_login()
                continue
            if form.step == "row-redirect-to-tassadar":
                form = self.submit_passthrough(form.step)
                continue
            if form.step == "initial-tou-agreement":
                if not self.state.data.get("countryProbed"):
                    form = self.submit_initial_country_probe(country)
                if form.step == "initial-tou-agreement":
                    form = self.submit_initial_tou(country)
                continue
            if form.step == "get-started" and country_probe and not self.state.data.get("countryProbed"):
                form = self.submit_country_probe(country)
            form = self.submit_step(form.step, country=country, opt_in=opt_in)
        return form

    def sync_from_browser(
        self,
        browser_cookies: Sequence[Mapping[str, Any]],
        *,
        form_html: str = "",
        form_url: str = "",
    ) -> Optional[FormSnapshot]:
        merge_playwright_cookies(self.session.cookies, browser_cookies)
        current = self.form
        if form_html:
            current = parse_flow_form(form_html, form_url or self.entry_url)
        if current is not None and current.step != "captcha-gate":
            raise RuntimeError(f"browser returned unexpected registration form: {current.step}")
        self.state.checkpoint(
            "captcha-gate",
            form=current.to_dict() if current else self.state.data.get("form"),
            cookies=self.cookie_records(),
            event={"completed": "browser-sync", "next": "captcha-gate"},
        )
        return current

    def submit_captcha(self, token: str) -> dict[str, Any]:
        form = self.form
        if form is None or form.step != "captcha-gate":
            raise RuntimeError("captcha submission requires the persisted captcha-gate form")
        fields = build_step_fields(
            form,
            "captcha-gate",
            self.state.data["identity"],
            arkose_token=str(token),
        )
        response = self._fragment_request("POST", form, fields)
        meta = self._save_response("captcha_gate_submit", response)
        outcome = classify_registration_response(
            response.text,
            self.state.data["identity"].get("email", ""),
        )
        status = "complete" if outcome["status"] == "success" else "captcha-gate"
        next_form: Optional[FormSnapshot] = None
        if status != "complete":
            try:
                next_form = parse_flow_form(response.text, self.entry_url)
            except ValueError:
                pass
        self.state.checkpoint(
            status,
            form=next_form.to_dict() if next_form else self.state.data.get("form"),
            cookies=self.cookie_records(),
            response=meta,
            result=outcome,
            event={
                "completed": "captcha-gate" if status == "complete" else None,
                "failed": None if status == "complete" else "captcha-gate",
                "classification": outcome["status"],
                "httpStatus": int(response.status_code),
            },
        )
        return {**outcome, "httpStatus": int(response.status_code), "response": meta}


__all__ = [
    "BattleProtocolClient",
    "FLOW_STEPS",
    "FormControl",
    "FormSnapshot",
    "PersistentFlowState",
    "build_country_probe_fields",
    "build_initial_tou_fields",
    "build_step_fields",
    "classify_registration_response",
    "detect_arkose_context",
    "encode_multipart",
    "merge_playwright_cookies",
    "parse_flow_form",
    "playwright_cookies_from_records",
    "serialize_cookie_jar",
    "step_from_action",
    "validate_transition",
]
