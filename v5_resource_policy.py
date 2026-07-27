"""V5 browser resource policy for low-value tracking traffic."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit


BLOCKED_TRACKING_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "rum.battle.net",
)


def should_block_tracking_resource(url: str) -> bool:
    host = (urlsplit(str(url or "")).hostname or "").strip(".").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in BLOCKED_TRACKING_HOSTS)


def install_ruyi_tracking_filter(page: Any, logger: logging.Logger) -> bool:
    """Install a request filter on a RuyiPage browser without affecting failures."""

    def handler(request: Any) -> None:
        url = str(getattr(request, "url", "") or "")
        if should_block_tracking_resource(url):
            request.fail()
        else:
            request.continue_request()

    try:
        page.intercept.start_requests(handler)
        logger.info(
            "V5 tracking filter enabled: %s",
            ", ".join(BLOCKED_TRACKING_HOSTS),
        )
        return True
    except Exception as exc:
        logger.warning(
            "V5 tracking filter unavailable: %s: %s",
            type(exc).__name__,
            exc,
        )
        return False


__all__ = [
    "BLOCKED_TRACKING_HOSTS",
    "install_ruyi_tracking_filter",
    "should_block_tracking_resource",
]
