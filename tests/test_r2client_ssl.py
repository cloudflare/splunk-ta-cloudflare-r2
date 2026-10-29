# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Unit tests for R2Client's SSL context construction -- no network required.

Covers the three verification postures the TA exposes via Account-tab
options (verify_ssl, ca_bundle_path, allow_nonstrict_tls):

  1. Default (verify_ssl=True, strict_tls=True): full verification, and
     VERIFY_X509_STRICT stays on wherever Python itself defaults it on.
  2. strict_tls=False: VERIFY_X509_STRICT is cleared, but CERT_REQUIRED /
     check_hostname are untouched -- this is deliberately narrower than
     verify_ssl=False.
  3. verify_ssl=False: CERT_NONE / check_hostname=False, regardless of
     strict_tls -- verify_ssl is the escape hatch of last resort and takes
     precedence.

Background: github.com/cloudflare/splunk-ta-cloudflare-r2/issues/3 -- Python
3.13+ turns on ssl.VERIFY_X509_STRICT by default in
ssl.create_default_context(), which some TLS-inspecting proxies' re-signed
certs fail (missing Authority Key Identifier), even though the cert is
otherwise trustworthy via a supplied CA bundle.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import ssl
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))

from r2client import R2Client


def _make_client(**overrides):
    kwargs = dict(
        account_id="abc123",
        access_key="AKIDFAKE",
        secret_key="secretfake",
    )
    kwargs.update(overrides)
    return R2Client(**kwargs)


class TestSSLContextDefaults(unittest.TestCase):

    def test_default_verifies_fully(self):
        client = _make_client()
        self.assertEqual(client._ssl_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client._ssl_ctx.check_hostname)

    def test_default_does_not_touch_verify_flags(self):
        # Whatever Python's own create_default_context() sets by default
        # (VERIFY_X509_STRICT on 3.13+, not set on older versions) should be
        # left alone when strict_tls is True (the default).
        baseline = ssl.create_default_context().verify_flags
        client = _make_client()
        self.assertEqual(client._ssl_ctx.verify_flags, baseline)


class TestStrictTlsFalse(unittest.TestCase):

    @unittest.skipUnless(
        hasattr(ssl, "VERIFY_X509_STRICT"),
        "ssl.VERIFY_X509_STRICT requires Python 3.13+",
    )
    def test_clears_verify_x509_strict_flag(self):
        client = _make_client(strict_tls=False)
        self.assertFalse(client._ssl_ctx.verify_flags & ssl.VERIFY_X509_STRICT)

    def test_still_requires_cert_and_hostname(self):
        # The whole point of strict_tls (vs. verify_ssl) is that it only
        # relaxes the strict X.509 profile check -- full chain and hostname
        # verification must remain on.
        client = _make_client(strict_tls=False)
        self.assertEqual(client._ssl_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client._ssl_ctx.check_hostname)

    def test_is_a_noop_on_python_versions_without_the_flag(self):
        # Defensive: hasattr guard in r2client.py must not raise on any
        # Python version Splunk might bundle.
        try:
            _make_client(strict_tls=False)
        except AttributeError:
            self.fail("strict_tls=False raised AttributeError on this Python version")


class TestVerifySslFalseTakesPrecedence(unittest.TestCase):

    def test_verify_ssl_false_disables_verification_regardless_of_strict_tls(self):
        for strict_tls in (True, False):
            with self.subTest(strict_tls=strict_tls):
                client = _make_client(verify_ssl=False, strict_tls=strict_tls)
                self.assertEqual(client._ssl_ctx.verify_mode, ssl.CERT_NONE)
                self.assertFalse(client._ssl_ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
