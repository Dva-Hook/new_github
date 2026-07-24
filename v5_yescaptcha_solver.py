from __future__ import annotations

import base64
import io
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import requests
from PIL import Image, ImageOps


DEFAULT_GET_TASK_RESULT_URL = "https://api.yescaptcha.com/getTaskResult"
PROGRESS_SUFFIX_RE = re.compile(r"\s*\(\s*\d+\s+of\s+\d+\s*\)\s*$", re.IGNORECASE)
QUESTION_SELECTORS = (
    "#root h2 span[role='text']",
    "#root h2 span",
    "h2 span[role='text']",
    "h2 span",
    "span[role='text']",
)
TRANSIENT_ERROR_CODES = frozenset(
    {
        "ERROR_SERVICE_UNAVALIABLE",
        "ERROR_SERVICE_UNAVAILABLE",
        "ERROR_PARSE_IMAGE_FAIL",
    }
)
PARSE_IMAGE_ERROR_CODE = "ERROR_PARSE_IMAGE_FAIL"
DEFAULT_RETRY_DELAYS = (0.5, 1.5)


def clean_prompt(value: str) -> str:
    """Remove only the round counter while preserving the live instruction."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    while True:
        cleaned = PROGRESS_SUFFIX_RE.sub("", text).strip()
        if cleaned == text:
            return text
        text = cleaned


def _question_js() -> str:
    selectors = json.dumps(list(QUESTION_SELECTORS))
    return f"""return (() => {{
      const selectors = {selectors};
      const roots = [document];
      const seen = new Set();
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      for (let i = 0; i < roots.length; i++) {{
        const root = roots[i];
        if (!root || seen.has(root)) continue;
        seen.add(root);
        try {{ root.querySelectorAll('*').forEach(el => {{ if (el.shadowRoot) roots.push(el.shadowRoot); }}); }} catch (e) {{}}
        for (const selector of selectors) {{
          let elements = [];
          try {{ elements = Array.from(root.querySelectorAll(selector)); }} catch (e) {{}}
          for (const element of elements) {{
            if (!visible(element)) continue;
            const text = (element.innerText || element.textContent || '').trim();
            if (text) return {{ok: true, selector, text}};
          }}
        }}
      }}
      return {{ok: false}};
    }})();"""


def extract_dynamic_prompt(
    page: Any,
    contexts_provider: Callable[[Any], Iterable[Any]],
    *,
    timeout: float = 4.0,
) -> dict[str, str]:
    """Read the current instruction from the live Arkose DOM for every wave."""

    deadline = time.monotonic() + max(0.1, float(timeout))
    script = _question_js()
    while time.monotonic() < deadline:
        for context in contexts_provider(page):
            try:
                result = context.run_js(
                    script,
                    timeout=min(1.0, max(0.1, deadline - time.monotonic())),
                )
            except Exception:
                continue
            if not isinstance(result, dict) or not result.get("ok"):
                continue
            prompt = clean_prompt(str(result.get("text") or ""))
            if prompt:
                return {
                    "prompt": prompt,
                    "selector": str(result.get("selector") or ""),
                }
        time.sleep(0.1)
    raise RuntimeError(
        "YesCaptcha dynamic question was not found in the live Arkose DOM"
    )


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"RIFF"):
        return "image/webp"
    return "image/jpeg"


def build_classification_payload(
    api_key: str,
    image: bytes,
    question: str,
) -> dict[str, Any]:
    return {
        "clientKey": api_key,
        "task": {
            "type": "FunCaptchaClassification",
            "image": (
                f"data:{image_mime(image)};base64,"
                f"{base64.b64encode(image).decode('ascii')}"
            ),
            "question": clean_prompt(question),
        },
    }


def reencode_rgb_jpeg(image: bytes) -> bytes:
    with Image.open(io.BytesIO(image)) as source:
        normalized = ImageOps.exif_transpose(source).convert("RGB")
        output = io.BytesIO()
        normalized.save(
            output,
            "JPEG",
            quality=92,
            optimize=False,
            progressive=False,
        )
    return output.getvalue()


def _json_response(response: Any) -> dict[str, Any]:
    result = response.json()
    if not isinstance(result, dict):
        raise RuntimeError("YesCaptcha returned a non-object JSON response")
    return result


def _answer_from_result(result: dict[str, Any]) -> int:
    if result.get("errorId") not in (None, 0):
        raise RuntimeError(
            "YesCaptcha failed: "
            f"errorId={result.get('errorId')} errorCode={result.get('errorCode')}"
        )
    solution = result.get("solution") or {}
    objects = solution.get("objects") or []
    if not objects:
        raise RuntimeError("YesCaptcha response has no solution.objects")
    try:
        answer = int(objects[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"YesCaptcha returned a non-integer answer: {objects[0]!r}"
        ) from exc
    if not 0 <= answer <= 11:
        raise RuntimeError(f"YesCaptcha answer index is outside 0..11: {answer}")
    return answer


def _get_result_url(create_url: str) -> str:
    if create_url.rstrip("/").endswith("/createTask"):
        return create_url.rstrip("/")[: -len("createTask")] + "getTaskResult"
    return DEFAULT_GET_TASK_RESULT_URL


def _transport_retry_code(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    status = int(getattr(response, "status_code", 0) or 0)
    if 500 <= status <= 599:
        return f"HTTP_{status}"
    if isinstance(exc, requests.Timeout):
        return "HTTP_TIMEOUT"
    if isinstance(exc, requests.ConnectionError):
        return "HTTP_CONNECTION_ERROR"
    return ""


def classify_image(
    image_path: Path,
    *,
    question: str,
    api_key: str,
    api_url: str,
    timeout: float,
    response_path: Path | None = None,
    session: Any = requests,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    if not api_key.strip():
        raise RuntimeError("YesCaptcha API key is empty")
    image = image_path.read_bytes()
    retry_reasons: list[str] = []
    attempt_records: list[dict[str, Any]] = []
    image_reencoded = False
    final_result: dict[str, Any] = {}

    for attempt in range(len(DEFAULT_RETRY_DELAYS) + 1):
        payload = build_classification_payload(api_key, image, question)
        transport_code = ""
        try:
            response = session.post(api_url, json=payload, timeout=float(timeout))
            response.raise_for_status()
            result = _json_response(response)
            final_result = result
            if result.get("status") != "ready" and result.get("taskId"):
                deadline = time.monotonic() + float(timeout)
                poll_url = _get_result_url(api_url)
                while time.monotonic() < deadline:
                    sleep(0.25)
                    poll = session.post(
                        poll_url,
                        json={"clientKey": api_key, "taskId": result["taskId"]},
                        timeout=float(timeout),
                    )
                    poll.raise_for_status()
                    final_result = _json_response(poll)
                    if (
                        final_result.get("status") == "ready"
                        or final_result.get("errorId") not in (None, 0)
                    ):
                        break
        except requests.RequestException as exc:
            transport_code = _transport_retry_code(exc)
            if not transport_code:
                raise
            final_result = {
                "errorId": 1,
                "errorCode": transport_code,
                "status": "transport-error",
            }

        error_code = transport_code or (
            str(final_result.get("errorCode") or "").strip()
            if final_result.get("errorId") not in (None, 0)
            else ""
        )
        attempt_records.append(
            {
                "attempt": attempt + 1,
                "status": final_result.get("status"),
                "errorCode": error_code,
                "imageReencoded": image_reencoded,
            }
        )
        retryable = error_code in TRANSIENT_ERROR_CODES or bool(transport_code)
        if error_code == PARSE_IMAGE_ERROR_CODE:
            retryable = retryable and not image_reencoded
            if retryable:
                image = reencode_rgb_jpeg(image)
                image_reencoded = True
        if not retryable or attempt >= len(DEFAULT_RETRY_DELAYS):
            break
        retry_reasons.append(error_code)
        sleep(DEFAULT_RETRY_DELAYS[attempt])

    retry_metadata = {
        "attempts": len(attempt_records),
        "retries": max(0, len(attempt_records) - 1),
        "reasons": retry_reasons,
        "imageReencoded": image_reencoded,
        "attemptRecords": attempt_records,
    }
    if response_path is not None:
        response_path.parent.mkdir(parents=True, exist_ok=True)
        saved_result = dict(final_result)
        saved_result["_retry"] = retry_metadata
        response_path.write_text(
            json.dumps(saved_result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    answer = _answer_from_result(final_result)
    return answer, {
        "answer": answer,
        "status": final_result.get("status"),
        "labels": (final_result.get("solution") or {}).get("labels"),
        **retry_metadata,
    }


__all__ = [
    "QUESTION_SELECTORS",
    "build_classification_payload",
    "clean_prompt",
    "classify_image",
    "extract_dynamic_prompt",
    "image_mime",
    "reencode_rgb_jpeg",
]
