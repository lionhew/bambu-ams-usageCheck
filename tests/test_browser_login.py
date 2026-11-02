"""Tests for browser login pure helpers."""

from __future__ import annotations

import unittest
from unittest import mock

from bambu_ams_check.browser_login import (
    CN_BROWSER_LOGIN_URL,
    browser_login_urls,
    extract_access_token,
    validate_and_resolve_user,
)
from bambu_ams_check.ams_cloud import CloudError, LoginResult


class TestExtractToken(unittest.TestCase):
    def test_from_cookie_header(self):
        self.assertEqual(
            extract_access_token("foo=1; token=abc.def.ghi.extra; bar=2"),
            "abc.def.ghi.extra",
        )

    def test_from_bearer(self):
        self.assertEqual(
            extract_access_token("Authorization: Bearer eyJhbGciOi.part2.part3xx"),
            "eyJhbGciOi.part2.part3xx",
        )

    def test_from_raw(self):
        tok = "x" * 40
        self.assertEqual(extract_access_token(tok), tok)

    def test_from_token_assignment(self):
        self.assertEqual(
            extract_access_token("access_token: eyJaaa.bbb.cccdddd"),
            "eyJaaa.bbb.cccdddd",
        )

    def test_empty(self):
        self.assertIsNone(extract_access_token(""))
        self.assertIsNone(extract_access_token(None))


class TestBrowserUrls(unittest.TestCase):
    def test_cn(self):
        u = browser_login_urls("cn")
        self.assertEqual(u.region, "cn")
        self.assertEqual(u.makerworld_login, CN_BROWSER_LOGIN_URL)
        self.assertIn("bambulab.cn/zh-cn/sign-in", u.makerworld_login)
        self.assertIn("makerworld.com.cn", u.makerworld_login)

    def test_us(self):
        u = browser_login_urls("us")
        self.assertEqual(u.region, "us")
        self.assertIn("makerworld.com", u.makerworld_home)
        self.assertNotIn("makerworld.com.cn", u.makerworld_home)


class TestValidateToken(unittest.TestCase):
    def test_validate_success(self):
        with mock.patch(
            "bambu_ams_check.browser_login.BambuCloudClient"
        ) as client_cls:
            inst = client_cls.return_value
            inst.user_id = ""
            inst.get_preference.return_value = {"uid": 4242, "name": "t"}
            result = validate_and_resolve_user("tokentokentokentoken1234", region="cn")
            self.assertIsInstance(result, LoginResult)
            self.assertEqual(result.user_id, "4242")
            self.assertEqual(result.access_token, "tokentokentokentoken1234")

    def test_validate_failure(self):
        with mock.patch(
            "bambu_ams_check.browser_login.BambuCloudClient"
        ) as client_cls:
            inst = client_cls.return_value
            inst.get_preference.side_effect = CloudError("nope")
            with mock.patch("bambu_ams_check.browser_login.requests.get") as get:
                resp = mock.Mock()
                resp.status_code = 401
                resp.text = "denied"
                get.return_value = resp
                with self.assertRaises(CloudError):
                    validate_and_resolve_user("badtokenbadtokenbadtoken", region="us")


if __name__ == "__main__":
    unittest.main()
