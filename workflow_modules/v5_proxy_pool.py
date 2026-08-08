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
        raise ValueError("代理端口必须是整数") from exc
    if not host or not 1 <= port <= 65535:
        raise ValueError("代理主机或端口无效")
    if not username or not password:
        raise ValueError("必须提供代理用户名和密码")
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
        raise ValueError("代理行为空")

    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
            raise ValueError(f"不支持的代理协议：{parsed.scheme}")
        if not parsed.hostname or not parsed.port:
            raise ValueError("代理 URL 缺少主机或端口")
        if parsed.username is None or parsed.password is None:
            raise ValueError("代理 URL 缺少用户名或密码")
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
                    "应使用 ip:端口:用户名:密码 或 用户名:密码@ip:端口 格式"
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
                    f"第 {line_number} 行：与第 {previous} 行的代理重复"
                )
                continue
            seen[record.url] = line_number
            records.append(record)
        except ValueError as exc:
            errors.append(f"第 {line_number} 行：{exc}")
    if errors:
        sample = "; ".join(errors[:5])
        raise ValueError(f"无效代理条目（{len(errors)}）：{sample}")
    if not records:
        raise ValueError(f"代理池为空：{path}")
    return records


def proxy_for_job(path: Path, one_based_index: int) -> ProxyRecord:
    if one_based_index < 1:
        raise ValueError("任务序号必须从 1 开始")
    records = load_proxy_pool(path)
    if one_based_index > len(records):
        raise ValueError(
            f"任务序号 {one_based_index} 超过代理池数量 {len(records)}"
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
        raise ValueError("任务数量必须为正数")
    if not 1 <= one_based_index <= job_count:
        raise ValueError(
            f"任务序号 {one_based_index} 超过配置的任务数量 "
            f"{job_count}"
        )
    if len(records) < job_count:
        raise ValueError(
            f"代理池有 {len(records)} 条记录，但请求了 "
            f"{job_count} 个任务"
        )
    if max_candidates < 1:
        raise ValueError("最大候选数必须为正数")
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
        raise ValueError("探测超时时间必须为正数")
    if not urls:
        raise ValueError("至少需要一个探测 URL")

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
            f"任务 {one_based_index} 没有可连通代理；"
            f"已测试 {len(attempts)} 个独立候选"
        ),
        attempts,
    )


def _format_checks(result: ProxyProbeResult) -> str:
    values = []
    for check in result.checks:
        outcome = str(check.status) if check.status is not None else check.error
        values.append(f"{check.target}={outcome}@{check.elapsed_ms}ms")
    return ",".join(values) or "无检查结果"


def _append_github_env(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_ENV")
    if not target:
        raise RuntimeError("未设置 GITHUB_ENV")
    with Path(target).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="校验或分配 V5 代理池")
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
            f"代理池有 {len(records)} 条记录，但请求了 {args.require} 个任务"
        )
    if args.index is None:
        print(f"代理池校验通过：数量={len(records)}")
        return 0

    if args.preflight:
        if args.job_count is None:
            raise SystemExit("使用 --preflight 时必须提供 --job-count")
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
            state = "通过" if result.ok else "失败"
            print(
                f"代理预检{state}：任务={args.index}，"
                f"源行={candidate.source_line}，端点={candidate.display}，"
                f"检查={_format_checks(result)}"
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
        f"代理已分配：任务={args.index}，源行={record.source_line}，"
        f"端点={record.display}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
