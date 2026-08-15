# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Unit tests for the pure, dependency-free helper functions in
package/bin/cloudflare_r2_helper.py: _as_bool, _normalize_prefix, and
_window_floor.

These three functions have no I/O and no Splunk/R2 dependency, so they're
tested directly and deterministically (no mocking of KV Store, R2, or
Splunk's SDKs needed). See tests/_splunk_stubs.py for why a stub step is
still required just to *import* the module at all (its other imports pull in
UCC/Splunk-supplied packages this test never touches).

Run:  python3 tests/test_helper_pure_functions.py        (from the repo root)
  or  python3 -m unittest discover -s tests -v
"""

import datetime
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))

from _splunk_stubs import install_stubs  # noqa: E402

install_stubs()

from cloudflare_r2_helper import (  # noqa: E402
    _as_bool,
    _build_proxy_url,
    _normalize_prefix,
    _r2_error_hint,
    _window_floor,
)
from r2client import R2Error  # noqa: E402


class TestAsBool(unittest.TestCase):

    def test_none_returns_default_true(self):
        self.assertTrue(_as_bool(None))

    def test_none_returns_default_false_when_specified(self):
        self.assertFalse(_as_bool(None, default=False))

    def test_true_strings(self):
        for v in ("true", "True", "TRUE", "1", "yes", "anything-else"):
            self.assertTrue(_as_bool(v), "expected {!r} -> True".format(v))

    def test_false_strings(self):
        for v in ("false", "False", "FALSE", "0", "no", "No", ""):
            self.assertFalse(_as_bool(v), "expected {!r} -> False".format(v))

    def test_whitespace_is_stripped(self):
        self.assertFalse(_as_bool("  false  "))
        self.assertTrue(_as_bool("  true  "))

    def test_case_insensitive(self):
        self.assertFalse(_as_bool("FaLsE"))
        self.assertFalse(_as_bool("nO"))

    def test_non_string_input_is_stringified_first(self):
        # Splunk config values normally arrive as strings, but the function's
        # str(value) call means real bool/int inputs behave sensibly too.
        self.assertFalse(_as_bool(0))
        self.assertTrue(_as_bool(1))
        self.assertTrue(_as_bool(True))
        # str(False) == "False", which IS in the falsy set - confirms the
        # function doesn't just check Python truthiness of the raw value.
        self.assertFalse(_as_bool(False))


class TestNormalizePrefix(unittest.TestCase):

    def test_none_returns_empty_string(self):
        self.assertEqual(_normalize_prefix(None), "")

    def test_empty_string_returns_empty_string(self):
        self.assertEqual(_normalize_prefix(""), "")

    def test_whitespace_only_returns_empty_string(self):
        self.assertEqual(_normalize_prefix("   "), "")

    def test_adds_trailing_slash_when_missing(self):
        self.assertEqual(_normalize_prefix("gateway_dns"), "gateway_dns/")

    def test_preserves_existing_trailing_slash(self):
        self.assertEqual(_normalize_prefix("gateway_dns/"), "gateway_dns/")

    def test_strips_surrounding_whitespace_before_adding_slash(self):
        self.assertEqual(_normalize_prefix("  gateway_dns  "), "gateway_dns/")

    def test_nested_prefix_without_trailing_slash(self):
        self.assertEqual(_normalize_prefix("a/b/c"), "a/b/c/")

    def test_nested_prefix_with_trailing_slash(self):
        self.assertEqual(_normalize_prefix("a/b/c/"), "a/b/c/")


class TestWindowFloor(unittest.TestCase):

    def _utc(self, y, m, d, hh=0, mm=0, ss=0):
        return datetime.datetime(y, m, d, hh, mm, ss, tzinfo=datetime.timezone.utc)

    def test_basic_one_day_lookback_no_prefix(self):
        now = self._utc(2026, 7, 5, 12, 0, 0)
        self.assertEqual(
            _window_floor("", 1, now=now), "20260704/20260704T120000Z"
        )

    def test_with_prefix_precedes_date_folder(self):
        now = self._utc(2026, 7, 5, 0, 0, 0)
        self.assertEqual(
            _window_floor("gateway_dns/", 7, now=now),
            "gateway_dns/20260628/20260628T000000Z",
        )

    def test_zero_lookback_days_floor_equals_now(self):
        # validate_input() rejects lookback_days < 1 at the input-config
        # layer, but _window_floor itself is pure date math - confirm it
        # still behaves correctly (floor == now) rather than raising or
        # producing an off-by-one, in case that guard is ever bypassed.
        now = self._utc(2026, 7, 5, 9, 30, 15)
        self.assertEqual(
            _window_floor("", 0, now=now), "20260705/20260705T093015Z"
        )

    def test_multi_day_lookback(self):
        now = self._utc(2026, 7, 15, 6, 0, 0)
        self.assertEqual(
            _window_floor("", 30, now=now), "20260615/20260615T060000Z"
        )

    def test_month_boundary_non_leap_year(self):
        # 2026 is not a leap year (2026 / 4 has a remainder) - Feb has 28 days.
        now = self._utc(2026, 3, 1, 0, 0, 0)
        self.assertEqual(
            _window_floor("", 1, now=now), "20260228/20260228T000000Z"
        )

    def test_month_boundary_leap_year(self):
        # 2024 is a leap year - Feb has 29 days.
        now = self._utc(2024, 3, 1, 0, 0, 0)
        self.assertEqual(
            _window_floor("", 1, now=now), "20240229/20240229T000000Z"
        )

    def test_year_boundary(self):
        now = self._utc(2026, 1, 1, 0, 0, 0)
        self.assertEqual(
            _window_floor("", 1, now=now), "20251231/20251231T000000Z"
        )

    def test_second_precision_is_preserved(self):
        now = self._utc(2026, 7, 5, 23, 59, 59)
        self.assertEqual(
            _window_floor("", 1, now=now), "20260704/20260704T235959Z"
        )

    def test_defaults_to_real_now_when_omitted(self):
        # Can't assert an exact value against real wall-clock time, but can
        # confirm the function doesn't crash without `now` and produces a
        # syntactically well-formed floor string.
        result = _window_floor("some_prefix/", 1)
        self.assertRegex(
            result, r"^some_prefix/\d{8}/\d{8}T\d{6}Z$"
        )


class TestBuildProxyUrl(unittest.TestCase):
    """_build_proxy_url() turns a resolved [proxy] stanza dict into an
    http://[user:pass@]host:port URL for urllib's ProxyHandler. See
    _get_proxy_settings/_build_proxy_url's docstrings in
    cloudflare_r2_helper.py for why the scheme is always "http://" here
    regardless of the traffic being proxied (that's the proxy's own listener
    protocol, a separate question from what's inside the tunnel)."""

    def test_host_and_port_only(self):
        proxy = {"proxy_url": "proxy.example.com", "proxy_port": "8080"}
        self.assertEqual(
            _build_proxy_url(proxy), "http://proxy.example.com:8080"
        )

    def test_with_username_and_password(self):
        proxy = {
            "proxy_url": "proxy.example.com",
            "proxy_port": "8080",
            "proxy_username": "alice",
            "proxy_password": "hunter2",
        }
        self.assertEqual(
            _build_proxy_url(proxy),
            "http://alice:hunter2@proxy.example.com:8080",
        )

    def test_username_only_no_password(self):
        proxy = {
            "proxy_url": "proxy.example.com",
            "proxy_port": "8080",
            "proxy_username": "alice",
            "proxy_password": "",
        }
        self.assertEqual(
            _build_proxy_url(proxy), "http://alice@proxy.example.com:8080"
        )

    def test_credentials_with_reserved_characters_are_percent_encoded(self):
        # A literal ':' or '@' in a username/password would otherwise be
        # misread as the URL's own user:pass@host delimiters.
        proxy = {
            "proxy_url": "proxy.example.com",
            "proxy_port": "3128",
            "proxy_username": "dom\\alice",
            "proxy_password": "p@ss:word",
        }
        url = _build_proxy_url(proxy)
        self.assertEqual(
            url, "http://dom%5Calice:p%40ss%3Aword@proxy.example.com:3128"
        )

    def test_missing_host_returns_none(self):
        self.assertIsNone(_build_proxy_url({"proxy_port": "8080"}))

    def test_missing_port_returns_none(self):
        self.assertIsNone(_build_proxy_url({"proxy_url": "proxy.example.com"}))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(_build_proxy_url({}))

    def test_whitespace_in_host_and_credentials_is_stripped(self):
        proxy = {
            "proxy_url": "  proxy.example.com  ",
            "proxy_port": " 8080 ",
            "proxy_username": " alice ",
            "proxy_password": " hunter2 ",
        }
        self.assertEqual(
            _build_proxy_url(proxy),
            "http://alice:hunter2@proxy.example.com:8080",
        )


class TestR2ErrorHint(unittest.TestCase):
    """_r2_error_hint() translates known R2/S3 error codes into an
    actionable log hint. The specific codes covered here were each verified
    against live R2 to be genuinely distinguishable from one another (not
    the same generic failure surfacing under different names) - see the
    comment above _R2_ERROR_HINTS in cloudflare_r2_helper.py."""

    def test_known_codes_return_a_nonempty_hint(self):
        for code in (
            "SignatureDoesNotMatch",
            "InvalidAccessKeyId",
            "RequestTimeTooSkewed",
            "NoSuchBucket",
            "AccessDenied",
        ):
            exc = R2Error(403, code, "<xml/>")
            hint = _r2_error_hint(exc)
            self.assertTrue(hint, "expected a non-empty hint for {!r}".format(code))

    def test_signature_does_not_match_hint_mentions_secret_key(self):
        exc = R2Error(403, "SignatureDoesNotMatch", "<xml/>")
        hint = _r2_error_hint(exc)
        self.assertIn("Secret Access Key", hint)

    def test_request_time_too_skewed_hint_mentions_clock(self):
        exc = R2Error(403, "RequestTimeTooSkewed", "<xml/>")
        hint = _r2_error_hint(exc)
        self.assertIn("clock", hint.lower())

    def test_unknown_code_returns_empty_string(self):
        exc = R2Error(500, "InternalError", "<xml/>")
        self.assertEqual(_r2_error_hint(exc), "")

    def test_non_r2error_exception_returns_empty_string(self):
        # Call sites catch a broader tuple (R2Error, OSError, EOFError,
        # zlib.error) and call this unconditionally - a plain exception with
        # no .code attribute must not raise, just return no hint.
        self.assertEqual(_r2_error_hint(OSError("connection reset")), "")
        self.assertEqual(_r2_error_hint(ValueError("whatever")), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
