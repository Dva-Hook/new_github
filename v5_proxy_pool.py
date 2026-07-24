# -*- coding: utf-8 -*-
"""Parse and allocate one proxy per V5 matrix job."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit


_HOST_PORT_USER_PASS = re.compile(
    r"^(?P<host>\[[^]]+\]|[^:\s]+):(?P<port>\d+):(?P<user>[^:\s]+):(?P<password>.+)$"
)


@dataclass(frozen=True)
class ProxyRecord:
    source_line: int
    url: str
    display: str


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
