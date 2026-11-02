"""Unit tests for Chromium CDP cookie helpers (mocked; no real browser)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from bambu_ams_check.cdp_browser import (
    build_chromium_launch_args,
    find_chromium_executable,
    find_free_port,
    parse_cdp_targets,
    pick_page_websocket_url,
    poll_token_from_cdp,
    token_from_cdp_cookies,
    token_from_document_cookie,
    decode_cdp_ws_payload,
)


class TestTokenFromCookies(unittest.TestCase):
    def test_token_from_cdp_cookie_list(self):
        cookies = [
            {"name": "session", "value": "x", "domain": ".makerworld.com.cn"},
            {
                "name": "token",
                "value": "eyJhbGciOi.part2.part3tokenvalue",
                "domain": ".makerworld.com.cn",
                "path": "/",
            },
        ]
        self.assertEqual(
            token_from_cdp_cookies(cookies, domain_substr="makerworld.com.cn"),
            "eyJhbGciOi.part2.part3tokenvalue",
        )

    def test_ignores_other_domains(self):
        cookies = [
            {"name": "token", "value": "wrong-site-token-xxxxxxxxxxxx", "domain": ".evil.example"},
            {"name": "token", "value": "good-token-value-yyyyyyyyyyyy", "domain": "makerworld.com.cn"},
        ]
        self.assertEqual(
            token_from_cdp_cookies(cookies, domain_substr="makerworld"),
            "good-token-value-yyyyyyyyyyyy",
        )

    def test_missing_token(self):
        self.assertIsNone(
            token_from_cdp_cookies(
                [{"name": "other", "value": "1", "domain": ".makerworld.com.cn"}],
                domain_substr="makerworld",
            )
        )

    def test_document_cookie_string(self):
        self.assertEqual(
            token_from_document_cookie("a=1; token=abc.def.ghi.jjjj; b=2"),
            "abc.def.ghi.jjjj",
        )


class TestCdpJsonHelpers(unittest.TestCase):
    def test_parse_targets(self):
        raw = [
            {
                "type": "page",
                "url": "https://makerworld.com.cn/zh",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/ABC",
            },
            {
                "type": "service_worker",
                "url": "https://makerworld.com.cn/sw.js",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/worker/X",
            },
        ]
        pages = parse_cdp_targets(raw)
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["webSocketDebuggerUrl"], "ws://127.0.0.1:9222/devtools/page/ABC")

    def test_pick_page_prefers_makerworld(self):
        targets = [
            {
                "type": "page",
                "url": "https://bambulab.cn/zh-cn/sign-in",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/a",
            },
            {
                "type": "page",
                "url": "https://makerworld.com.cn/zh",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/b",
            },
            {
                "type": "page",
                "url": "http://127.0.0.1:8765/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/c",
            },
        ]
        self.assertEqual(
            pick_page_websocket_url(targets, prefer_substr="makerworld.com.cn"),
            "ws://127.0.0.1:9/b",
        )

    def test_pick_page_falls_back_to_any_page(self):
        targets = [
            {
                "type": "page",
                "url": "https://bambulab.cn/zh-cn/sign-in",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/a",
            },
        ]
        self.assertEqual(
            pick_page_websocket_url(targets, prefer_substr="makerworld.com.cn"),
            "ws://127.0.0.1:9/a",
        )

    def test_decode_cdp_result_payload(self):
        msg = {
            "id": 3,
            "result": {
                "cookies": [
                    {
                        "name": "token",
                        "value": "tok123tok123tok123tok12",
                        "domain": ".makerworld.com.cn",
                    }
                ]
            },
        }
        decoded = decode_cdp_ws_payload(json.dumps(msg))
        self.assertEqual(decoded["id"], 3)
        self.assertIn("cookies", decoded["result"])


class _FakeBin:
    def __init__(self, path: str, exists: bool) -> None:
        self._path = path
        self._exists = exists

    def is_file(self) -> bool:
        return self._exists

    def __str__(self) -> str:
        return self._path


class TestFindBrowser(unittest.TestCase):
    def test_find_chromium_uses_first_existing(self):
        chrome = _FakeBin(r"C:\fake\chrome.exe", False)
        edge = _FakeBin(r"C:\fake\msedge.exe", True)
        with mock.patch(
            "bambu_ams_check.cdp_browser._candidate_chromium_paths",
            return_value=[chrome, edge],
        ):
            found = find_chromium_executable()
        self.assertEqual(found, r"C:\fake\msedge.exe")

    def test_find_chromium_none(self):
        missing = _FakeBin(r"C:\missing\chrome.exe", False)
        with mock.patch(
            "bambu_ams_check.cdp_browser._candidate_chromium_paths",
            return_value=[missing],
        ):
            self.assertIsNone(find_chromium_executable())

    def test_launch_args_include_debug_and_profile(self):
        args = build_chromium_launch_args(
            executable="C:/Edge/msedge.exe",
            user_data_dir="E:/proj/.browser-profile",
            port=9333,
            url="https://example.com/login",
        )
        self.assertEqual(args[0], "C:/Edge/msedge.exe")
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("https://example.com/login", args)
        self.assertIn("--no-first-run", args)
        joined = " ".join(args)
        self.assertNotIn("User Data\\Default", joined)

    def test_find_free_port_returns_int(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 0)


class TestPollToken(unittest.TestCase):
    def test_poll_returns_when_reader_finds_token(self):
        targets = [
            {
                "type": "page",
                "url": "https://makerworld.com.cn/zh",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9/b",
            }
        ]
        sleeps: list[float] = []

        def fake_read(ws_url, *, cookie_urls, domain_substr):
            self.assertEqual(ws_url, "ws://127.0.0.1:9/b")
            self.assertTrue(cookie_urls)
            self.assertEqual(domain_substr, "makerworld")
            return "polled-token-value-xxxxxxxxxxxx"

        token = poll_token_from_cdp(
            9222,
            prefer_url_substr="makerworld.com.cn",
            cookie_urls=["https://makerworld.com.cn/"],
            domain_substr="makerworld",
            timeout_seconds=5.0,
            poll_interval=0.01,
            sleep_fn=sleeps.append,
            fetch_targets=lambda _port: targets,
            read_token=fake_read,
        )
        self.assertEqual(token, "polled-token-value-xxxxxxxxxxxx")

    def test_poll_stops_when_should_stop(self):
        calls = {"n": 0}

        def fetch(_port):
            calls["n"] += 1
            return []

        token = poll_token_from_cdp(
            9222,
            prefer_url_substr="makerworld.com.cn",
            cookie_urls=["https://makerworld.com.cn/"],
            timeout_seconds=30.0,
            poll_interval=0.01,
            should_stop=lambda: True,
            sleep_fn=lambda _s: None,
            fetch_targets=fetch,
            read_token=lambda *a, **k: None,
        )
        self.assertIsNone(token)
        self.assertEqual(calls["n"], 0)


if __name__ == "__main__":
    unittest.main()
