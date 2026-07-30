# -*- coding: utf-8 -*-
"""Parse and allocate one proxy per V5 matrix job."""

from __future__ import annotations

import argparse
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit


_HOST_PORT_USER_PASS = re.compile(
    r"^(?P<host>\[[^]]+\]|[^:\s]+):(?P<port>\d+):(?P<user>[^:\s]+):(?P<password>.+)$"
)
DEFAULT_PREFLIGHT_URLS = (
    "https://account.battle.net/robots.txt",
    "https://blizzard-api.arkoselabs.com/",
)


@dataclass(frozen=True)
class ProxyRecord:
    source_line: int
    url: str
    display: str


@dataclass(frozen=True)
class ProxyProbeCheck:
    target: str
    ok: bool
    elapsed_ms: int
    status: int | None = None
    error: str = ""


@dataclass(frozen=True)
class ProxyProbeResult:
    ok: bool
    checks: tuple[ProxyProbeCheck, ...]


class ProxyAllocationError(RuntimeError):
    def __init__(
        self,
        message: str,
        attempts: Sequence[tuple[ProxyRecord, ProxyProbeResult]],
    ) -> None:
        super().__init__(message)
        self.attempts = list(attempts)


def _build_url(host: str, port_text: str, username: str, password: str) -> ProxyRecord:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("proxy port must be an integer") from exc
    if not host or not 1 <= port <= 65535:
        raise ValueError("proxy host or port is invalid")
    if not username or not password:
        raise ValueError("proxy username and password are required")
    url_host = f"[{host}]" if ":" in host else host
    userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return ProxyRecord(
        source_line=0,
        url=urlunsplit(("http", f"{userinfo}{url_host}:{port}", "", "", "")),
        display=f"http://{url_host}:{port}",
    )


def parse_proxy_line(raw: str, source_line: int = 0) -> ProxyRecord:
    value = str(raw or "").strip().rstrip(",").strip()
    if not value:
        raise ValueError("proxy line is blank")

    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"unsupported proxy scheme: {parsed.scheme}")
        if not parsed.hostname or not parsed.port:
            raise ValueError("proxy URL has no host or port")
        if parsed.username is None or parsed.password is None:
            raise ValueError("proxy URL has no username/password")
        record = _build_url(
            parsed.hostname,
            str(parsed.port),
            unquote(parsed.username),
            unquote(parsed.password),
        )
        if parsed.scheme.lower() != "http":
            record = ProxyRecord(
                source_line=0,
                url=record.url.replace("http://", f"{parsed.scheme.lower()}://", 1),
                display=record.display.replace("http://", f"{parsed.scheme.lower()}://", 1),
            )
    else:
        match = _HOST_PORT_USER_PASS.fullmatch(value)
        if match:
            record = _build_url(
                match.group("host"),
                match.group("port"),
                match.group("user"),
                match.group("password"),
            )
        else:
            # Existing proxy exports commonly use username:password@host:port.
            parsed = urlsplit("http://" + value)
            if (
                parsed.username is None
                or parsed.password is None
                or not parsed.hostname
                or not parsed.port
            ):
                raise ValueError(
                    "expected ip:port:username:password or username:password@ip:port"
                )
            record = _build_url(
                parsed.hostname,
                str(parsed.port),
                unquote(parsed.username),
                unquote(parsed.password),
            )
    return ProxyRecord(source_line=source_line, url=record.url, display=record.display)


def load_proxy_pool(path: Path) -> list[ProxyRecord]:
    records: list[ProxyRecord] = []
    errors: list[str] = []
    seen: dict[str, int] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            record = parse_proxy_line(stripped, line_number)
            previous = seen.get(record.url)
            if previous is not None:
                errors.append(
                    f"line {line_number}: duplicate proxy from line {previous}"
                )
                continue
            seen[record.url] = line_number
            records.append(record)
        except ValueError as exc:
            errors.append(f"line {line_number}: {exc}")
    if errors:
        sample = "; ".join(errors[:5])
        raise ValueError(f"invalid proxy entries ({len(errors)}): {sample}")
    if not records:
        raise ValueError(f"proxy pool is empty: {path}")
    return records


def proxy_for_job(path: Path, one_based_index: int) -> ProxyRecord:
    if one_based_index < 1:
        raise ValueError("job index must be one-based")
    records = load_proxy_pool(path)
    if one_based_index > len(records):
        raise ValueError(
            f"job index {one_based_index} exceeds proxy pool size {len(records)}"
        )
    return records[one_based_index - 1]


def proxy_candidates_for_job(
    records: Sequence[ProxyRecord],
    one_based_index: int,
    job_count: int,
    *,
    max_candidates: int = 5,
) -> list[ProxyRecord]:
    """Return a collision-free candidate shard for one matrix job."""
    if job_count < 1:
        raise ValueError("job count must be positive")
    if not 1 <= one_based_index <= job_count:
        raise ValueError(
            f"job index {one_based_index} is outside the configured "
            f"job count {job_count}"
        )
    if len(records) < job_count:
        raise ValueError(
            f"proxy pool has {len(records)} entries, but "
            f"{job_count} jobs were requested"
        )
    if max_candidates < 1:
        raise ValueError("max candidates must be positive")
    return list(records[one_based_index - 1 :: job_count][:max_candidates])


def _safe_probe_error(exc: BaseException) -> str:
    parts = [type(exc).__name__]
    for attribute in ("code", "errno"):
        value = getattr(exc, attribute, None)
        if isinstance(value, (int, str)) and str(value).strip():
            parts.append(f"{attribute}={value}")
    return ":".join(parts)


def probe_proxy(
    record: ProxyRecord,
    urls: Sequence[str] = DEFAULT_PREFLIGHT_URLS,
    timeout: float = 8.0,
) -> ProxyProbeResult:
    """Verify target HTTPS/TLS reachability through one proxy."""
    if timeout <= 0:
        raise ValueError("probe timeout must be positive")
    if not urls:
        raise ValueError("at least one probe URL is required")

    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate="chrome")
    checks: list[ProxyProbeCheck] = []
    proxies = {"http": record.url, "https": record.url}
    try:
        for url in urls:
            target = urlsplit(url).netloc or str(url)
            started = time.monotonic()
            response = None
            try:
                response = session.get(
                    url,
                    proxies=proxies,
                    timeout=float(timeout),
                    allow_redirects=False,
                    headers={
                        "Accept": "text/plain,*/*;q=0.1",
                        "Range": "bytes=0-0",
                    },
                )
                status = int(response.status_code)
                ok = 100 <= status < 500 and status != 407
                checks.append(
                    ProxyProbeCheck(
                        target=target,
                        ok=ok,
                        status=status,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        error="" if ok else f"http_status={status}",
                    )
                )
            except Exception as exc:
                checks.append(
                    ProxyProbeCheck(
                        target=target,
                        ok=False,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        error=_safe_probe_error(exc),
                    )
                )
            finally:
                if response is not None:
                    response.close()
            if not checks[-1].ok:
                break
    finally:
        session.close()
    return ProxyProbeResult(
        ok=bool(checks) and all(check.ok for check in checks),
        checks=tuple(checks),
    )


def allocate_healthy_proxy(
    path: Path,
    one_based_index: int,
    job_count: int,
    *,
    max_candidates: int = 5,
    urls: Sequence[str] = DEFAULT_PREFLIGHT_URLS,
    timeout: float = 8.0,
    probe: Callable[
        [ProxyRecord, Sequence[str], float], ProxyProbeResult
    ] = probe_proxy,
) -> tuple[ProxyRecord, list[tuple[ProxyRecord, ProxyProbeResult]]]:
    records = load_proxy_pool(path)
    candidates = proxy_candidates_for_job(
        records,
        one_based_index,
        job_count,
        max_candidates=max_candidates,
    )
    attempts: list[tuple[ProxyRecord, ProxyProbeResult]] = []
    for record in candidates:
        result = probe(record, urls, timeout)
        attempts.append((record, result))
        if result.ok:
            return record, attempts
    raise ProxyAllocationError(
        (
            f"no reachable proxy for job {one_based_index}; "
            f"tested {len(attempts)} isolated candidates"
        ),
        attempts,
    )


def _format_checks(result: ProxyProbeResult) -> str:
    values = []
    for check in result.checks:
        outcome = str(check.status) if check.status is not None else check.error
        values.append(f"{check.target}={outcome}@{check.elapsed_ms}ms")
    return ",".join(values) or "no-checks"


def _append_github_env(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_ENV")
    if not target:
        raise RuntimeError("GITHUB_ENV is not set")
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or allocate a V5 proxy pool")
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--require", type=int, default=0)
    parser.add_argument("--index", type=int)
    parser.add_argument("--job-count", type=int)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--probe-timeout", type=float, default=8.0)
    parser.add_argument("--probe-url", action="append", default=[])
    parser.add_argument("--github-env", action="store_true")
    args = parser.parse_args()

    records = load_proxy_pool(args.file)
    if args.require and len(records) < args.require:
        raise SystemExit(
            f"proxy pool has {len(records)} entries, but {args.require} jobs were requested"
        )
    if args.index is None:
        print(f"proxy pool valid: count={len(records)}")
        return 0

    if args.preflight:
        if args.job_count is None:
            raise SystemExit("--job-count is required with --preflight")
        probe_urls = tuple(args.probe_url) or DEFAULT_PREFLIGHT_URLS
        allocation_error: ProxyAllocationError | None = None
        try:
            record, attempts = allocate_healthy_proxy(
                args.file,
                args.index,
                args.job_count,
                max_candidates=args.max_candidates,
                urls=probe_urls,
                timeout=args.probe_timeout,
            )
        except ProxyAllocationError as exc:
            attempts = exc.attempts
            allocation_error = exc
        for candidate, result in attempts:
            state = "passed" if result.ok else "failed"
            print(
                f"proxy preflight {state}: job={args.index} "
                f"source_line={candidate.source_line} endpoint={candidate.display} "
                f"checks={_format_checks(result)}"
            )
        if allocation_error is not None:
            raise SystemExit(str(allocation_error)) from allocation_error
    else:
        record = proxy_for_job(args.file, args.index)
    if args.github_env:
        print(f"::add-mask::{record.url}")
        _append_github_env("REGISTRATION_PROXY", record.url)
        _append_github_env("V5_PROXY_SOURCE_LINE", str(record.source_line))
    print(
        f"proxy allocated: job={args.index} source_line={record.source_line} "
        f"endpoint={record.display}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
