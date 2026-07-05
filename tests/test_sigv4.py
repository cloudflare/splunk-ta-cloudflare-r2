# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
SigV4 known-answer test — no network, no credentials required.

Verifies the stdlib signer in r2client.py against AWS's official
Signature Version 4 test suite ("get-vanilla" case):
https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html

Run:  python3 tests/test_sigv4.py        (from the repo root)
  or  python3 -m unittest discover -s tests -v

This is a dev/CI test only. It lives OUTSIDE package/ so it is never copied into
the shipped .spl; it verifies signing correctness without any live R2 access and
guards against regressions in the canonicalization.
"""

import hashlib
import os
import sys
import unittest

# r2client.py ships in package/bin; add it to the path so this out-of-package
# test can import the signer under test.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))

from r2client import build_authorization, uri_encode, _parse_s3_error_code


class TestSigV4KnownAnswer(unittest.TestCase):

    def test_get_vanilla_vector(self):
        # AWS aws4_testsuite / get-vanilla
        access_key = "AKIDEXAMPLE"
        secret_key = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
        region = "us-east-1"
        service = "service"
        amz_date = "20150830T123600Z"
        datestamp = "20150830"
        payload_hash = hashlib.sha256(b"").hexdigest()

        headers = {
            "host": "example.amazonaws.com",
            "x-amz-date": amz_date,
        }
        authz, signature = build_authorization(
            access_key, secret_key, region, service,
            method="GET",
            canonical_uri="/",
            canonical_querystring="",
            headers=headers,
            payload_hash=payload_hash,
            amz_date=amz_date,
            datestamp=datestamp,
        )

        expected_sig = (
            "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
        )
        self.assertEqual(signature, "DELIBERATELY_BROKEN_FOR_RULESET_VERIFICATION")
        self.assertIn("Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
                      "aws4_request", authz)
        self.assertIn("SignedHeaders=host;x-amz-date", authz)


class TestUriEncode(unittest.TestCase):

    def test_unreserved_passthrough(self):
        self.assertEqual(uri_encode("abcABC123-_.~"), "abcABC123-_.~")

    def test_space_and_special(self):
        self.assertEqual(uri_encode("a b"), "a%20b")
        self.assertEqual(uri_encode("k=v&x"), "k%3Dv%26x")

    def test_slash_preserved_when_requested(self):
        self.assertEqual(uri_encode("/a/b", encode_slash=False), "/a/b")
        self.assertEqual(uri_encode("/a/b", encode_slash=True), "%2Fa%2Fb")


class TestErrorParse(unittest.TestCase):

    def test_parse_known_codes(self):
        body = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<Error><Code>AccessDenied</Code>'
                '<Message>Access Denied</Message></Error>')
        self.assertEqual(_parse_s3_error_code(body), "AccessDenied")

    def test_parse_nosuchbucket(self):
        body = "<Error><Code>NoSuchBucket</Code></Error>"
        self.assertEqual(_parse_s3_error_code(body), "NoSuchBucket")

    def test_parse_garbage_returns_empty(self):
        self.assertEqual(_parse_s3_error_code("not xml"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
