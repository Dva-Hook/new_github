from __future__ import annotations

import logging
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from v5_email_verifier import (
    _wait_login_phase,
    close_cached_ruyi_browser,
    direct_battlenet_link,
    extract_battlenet_link,
    find_link,
    is_login_success_state,
    launch_cached_ruyi_browser,
    sanitize_cached_profile,
)
from v5_resource_policy import (
    install_ruyi_tracking_filter,
    should_block_tracking_resource,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse(self.payload)


class FakeLoginPage:
    def __init__(self, states: list[dict]) -> None:
        self.states = list(states)
        self.last = dict(states[-1])

    def run_js(self, *_args, **_kwargs) -> dict:
        if self.states:
            self.last = dict(self.states.pop(0))
        return dict(self.last)


class EmailVerifierTests(unittest.TestCase):
    def test_tracking_policy_blocks_known_trackers_but_keeps_forge(self) -> None:
        for url in (
            "https://www.googletagmanager.com/gtm.js?id=GTM-TEST",
            "https://www.google-analytics.com/g/collect",
            "https://stats.g.doubleclick.net/j/collect",
            "https://rum.battle.net/api/v1/collect",
        ):
            with self.subTest(url=url):
                self.assertTrue(should_block_tracking_resource(url))

        self.assertFalse(
            should_block_tracking_resource("https://forge.akamaized.net/app.js")
        )
        self.assertFalse(
            should_block_tracking_resource(
                "https://blizzard-api.arkoselabs.com/fc/gc/"
            )
        )

    def test_ruyi_tracking_filter_fails_only_matching_requests(self) -> None:
        installed: dict[str, object] = {}

        class FakeIntercept:
            def start_requests(self, handler) -> None:
                installed["handler"] = handler

        class FakePage:
            intercept = FakeIntercept()

        class FakeRequest:
            def __init__(self, url: str) -> None:
                self.url = url
                self.action = ""

            def fail(self) -> None:
                self.action = "fail"

            def continue_request(self) -> None:
                self.action = "continue"

        self.assertTrue(install_ruyi_tracking_filter(FakePage(), logging.getLogger()))
        handler = installed["handler"]

        blocked = FakeRequest("https://www.googletagmanager.com/gtm.js")
        handler(blocked)
        self.assertEqual(blocked.action, "fail")

        allowed = FakeRequest("https://forge.akamaized.net/app.js")
        handler(allowed)
        self.assertEqual(allowed.action, "continue")

    def test_cached_browser_lifecycle_starts_clean_and_finishes_clean(self) -> None:
        with TemporaryDirectory() as raw_dir:
            cache_dir = Path(raw_dir) / "managed-profile"
            cache_dir.mkdir()
            (cache_dir / ".v5_email_verification_profile").write_text(
                "managed\n", encoding="ascii"
            )
            (cache_dir / "cache2").mkdir()
            (cache_dir / "cache2" / "static.bin").write_bytes(b"static")
            (cache_dir / "cookies.sqlite").write_bytes(b"old-account")
            launch_options: dict = {}

            class FakeIntercept:
                def start_requests(self, handler) -> None:
                    launch_options["request_filter"] = handler

            class FakePage:
                intercept = FakeIntercept()

                def close_other_tabs(self) -> None:
                    return None

                def set_bypass_csp(self, _enabled: bool) -> None:
                    return None

                def quit(self, **_kwargs) -> None:
                    (cache_dir / "cookies.sqlite").write_bytes(b"new-account")
                    (cache_dir / "storage" / "default").mkdir(parents=True)

            def fake_launch(**kwargs):
                launch_options.update(kwargs)
                self.assertFalse((cache_dir / "cookies.sqlite").exists())
                self.assertTrue((cache_dir / "cache2" / "static.bin").exists())
                return FakePage()

            fake_module = SimpleNamespace(launch=fake_launch)
            args = SimpleNamespace(
                email_browser_cache_dir=str(cache_dir),
                headless=True,
            )
            proxy = SimpleNamespace(url=None)
            with patch.dict(sys.modules, {"ruyipage": fake_module}):
                page, actual_cache_dir = launch_cached_ruyi_browser(
                    args, proxy, None, Path(raw_dir) / "run"
                )

            self.assertEqual(actual_cache_dir, cache_dir.resolve())
            self.assertEqual(launch_options["user_dir"], str(cache_dir.resolve()))
            self.assertFalse(launch_options["private"])
            self.assertIn("request_filter", launch_options)
            close_cached_ruyi_browser(page, actual_cache_dir)
            self.assertFalse((cache_dir / "cookies.sqlite").exists())
            self.assertFalse((cache_dir / "storage" / "default").exists())
            self.assertTrue((cache_dir / "cache2" / "static.bin").exists())

    def test_profile_sanitizer_preserves_cache2_and_removes_identity_state(self) -> None:
        with TemporaryDirectory() as raw_dir:
            cache_dir = Path(raw_dir) / "managed-profile"
            cache_dir.mkdir()
            (cache_dir / ".v5_email_verification_profile").write_text(
                "managed\n", encoding="ascii"
            )
            (cache_dir / "cache2" / "entries").mkdir(parents=True)
            (cache_dir / "cache2" / "entries" / "static.bin").write_bytes(b"static")
            (cache_dir / "cookies.sqlite").write_bytes(b"cookie")
            (cache_dir / "logins.json").write_text("{}", encoding="utf-8")
            (cache_dir / "sessionstore-backups").mkdir()
            (cache_dir / "sessionstore-backups" / "previous.jsonlz4").write_bytes(
                b"session"
            )
            (cache_dir / "storage" / "default" / "account.example").mkdir(
                parents=True
            )
            (cache_dir / "storage" / "default" / "account.example" / "state").write_bytes(
                b"identity"
            )
            (cache_dir / "parent.lock").write_text("stale", encoding="ascii")

            removed = sanitize_cached_profile(cache_dir, lock_timeout=0)

            self.assertTrue((cache_dir / "cache2" / "entries" / "static.bin").is_file())
            self.assertFalse((cache_dir / "cookies.sqlite").exists())
            self.assertFalse((cache_dir / "logins.json").exists())
            self.assertFalse((cache_dir / "sessionstore-backups").exists())
            self.assertFalse((cache_dir / "storage" / "default").exists())
            self.assertFalse((cache_dir / "parent.lock").exists())
            self.assertIn("cookies.sqlite", removed)
            self.assertIn(str(Path("storage") / "default"), removed)

            self.assertEqual(sanitize_cached_profile(cache_dir, lock_timeout=0), [])

    def test_profile_sanitizer_rejects_unmanaged_nonempty_custom_directory(self) -> None:
        with TemporaryDirectory() as raw_dir:
            cache_dir = Path(raw_dir) / "unmanaged-profile"
            cache_dir.mkdir()
            (cache_dir / "keep.txt").write_text("unmanaged", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                sanitize_cached_profile(cache_dir, lock_timeout=0)

    def test_post_password_wait_ignores_visible_password_until_success(self) -> None:
        page = FakeLoginPage(
            [
                {
                    "password_input_present": True,
                    "login_form_present": True,
                },
                {
                    "password_input_present": True,
                    "login_form_present": True,
                },
                {
                    "password_input_present": False,
                    "login_form_present": False,
                    "protected_shell_present": True,
                },
            ]
        )
        phase, _state = _wait_login_phase(
            page,
            0.5,
            accept_password=False,
            poll_interval=0,
        )
        self.assertEqual(phase, "success")

    def test_pre_password_wait_accepts_password_step(self) -> None:
        page = FakeLoginPage(
            [{"password_input_present": True, "login_form_present": True}]
        )
        phase, _state = _wait_login_phase(
            page,
            0.5,
            accept_password=True,
            poll_interval=0,
        )
        self.assertEqual(phase, "password")

    def test_login_challenge_precedes_success_markers(self) -> None:
        state = {
            "security_challenge_present": True,
            "email_code_challenge_present": True,
            "protected_shell_present": True,
        }
        self.assertFalse(is_login_success_state(state))
        phase, _state = _wait_login_phase(
            FakeLoginPage([state]),
            0.5,
            accept_password=False,
            poll_interval=0,
        )
        self.assertEqual(phase, "manual_email_code")

    def test_explicit_login_error_is_not_reported_as_timeout(self) -> None:
        page = FakeLoginPage(
            [
                {
                    "password_input_present": True,
                    "login_form_present": True,
                    "login_error_text": "Invalid credentials",
                }
            ]
        )
        phase, state = _wait_login_phase(
            page,
            0.5,
            accept_password=False,
            poll_interval=0,
        )
        self.assertEqual(phase, "login_error")
        self.assertEqual(state["login_error_text"], "Invalid credentials")

    def test_extracts_direct_ticket_link(self) -> None:
        link = "https://account.battle.net/overview?ticket=abc123"
        message = {
            "body": {"content": f'<html><a href="{link}">Verify</a></html>'},
            "bodyPreview": "",
        }
        self.assertEqual(extract_battlenet_link(message), link)

    def test_extracts_nested_redirect_link(self) -> None:
        nested = (
            "https://example.com/click?url="
            "https%3A%2F%2Faccount.battle.net%2Foverview%3Fticket%3Dabc123"
        )
        self.assertEqual(
            direct_battlenet_link(nested),
            "https://account.battle.net/overview?ticket=abc123",
        )

    def test_rejects_non_ticket_links(self) -> None:
        self.assertIsNone(direct_battlenet_link("https://account.battle.net/overview"))
        self.assertIsNone(direct_battlenet_link("https://example.com/overview?ticket=x"))

    def test_find_link_ignores_old_or_wrong_sender_messages(self) -> None:
        link = "https://account.battle.net/overview?ticket=new-ticket"
        payload = {
            "value": [
                {
                    "from": {"emailAddress": {"address": "someone@example.com"}},
                    "receivedDateTime": "2026-07-26T01:30:00Z",
                    "body": {"content": link},
                },
                {
                    "from": {"emailAddress": {"address": "noreply@battle.net"}},
                    "receivedDateTime": "2026-07-25T01:30:00Z",
                    "body": {"content": "https://account.battle.net/overview?ticket=old"},
                },
                {
                    "from": {"emailAddress": {"address": "noreply@battle.net"}},
                    "receivedDateTime": "2026-07-26T01:31:00Z",
                    "body": {"content": link},
                },
            ]
        }
        found, scanned, matching = find_link(
            FakeSession(payload),
            "access-token",
            not_before=datetime(2026, 7, 26, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(found, link)
        self.assertEqual(scanned, 3)
        self.assertEqual(matching, 1)


if __name__ == "__main__":
    unittest.main()
