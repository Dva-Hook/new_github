from __future__ import annotations

import sys
from pathlib import Path

import v5_proxy_pool


def test_proxy_pool_writes_authenticated_url_without_printing_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    proxy_file = tmp_path / "IP.txt"
    output_file = tmp_path / "runner-temp" / "fallback-proxy"
    proxy_file.write_text("proxy.example:10000:user:secret\n", encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v5_proxy_pool.py",
            "--file",
            str(proxy_file),
            "--index",
            "1",
            "--output-file",
            str(output_file),
        ],
    )

    assert v5_proxy_pool.main() == 0
    assert output_file.read_text(encoding="utf-8") == (
        "http://user:secret@proxy.example:10000\n"
    )
    assert "secret" not in capsys.readouterr().out
