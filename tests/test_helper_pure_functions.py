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

from cloudflare_r2_helper import _as_bool, _normalize_prefix, _window_floor  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
