# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Pure standard-library client for Cloudflare R2 (S3-compatible API).
=================================================================

REFERENCE IMPLEMENTATION for the UCC-based TA. No third-party dependencies:
no boto3, no botocore, no urllib3. Only the Python standard library.

Why stdlib instead of boto3:
  - The TA needs exactly two S3 operations (ListObjectsV2, GetObject) plus
    SigV4 request signing. boto3/botocore is an enormous dependency for that.
  - boto3 1.43+ requires Python >=3.10, but Splunk 9.4/10.0/10.2 ship Python
    3.9, forcing a pin to boto3 1.42.97 (last 3.9 release) which is past its
    upstream security-support cutoff (2026-04-29). Shipping an unmaintained AWS
    SDK to regulated customers is a review liability.
  - Everything boto3 did for us is in the stdlib:
      SigV4     -> hmac + hashlib
      HTTPS     -> urllib.request / http.client (stdlib ssl)
      List XML  -> xml.etree.ElementTree
      gunzip    -> gzip

Validated (2026-07): the SigV4 signer reproduces AWS's official "get-vanilla"
known-answer vector exactly (see test_sigv4.py), and this client performed
PUT/ListObjectsV2/GetObject/DELETE against a live R2 bucket on both host
Python 3.14 and Splunk 10.0's bundled Python 3.9 (run with
LD_LIBRARY_PATH=/opt/splunk/lib so stdlib ssl loads Splunk's OpenSSL),
including production-shaped keys (<prefix>/<YYYYMMDD>/<START>_<END>_<hash>.log.gz).

Notes for the TA:
  - region is "auto" for R2; service is "s3"; addressing is path-style.
  - Signed headers: host, x-amz-content-sha256, x-amz-date.
  - verify_ssl / ca_bundle mirror the TA's input options. Default verify=True.
    A TLS-inspecting network (corporate MITM) will fail default verification;
    that is when verify_ssl=false or a ca_bundle is required.
  - strict_tls mirrors the TA's "allow_nonstrict_tls" account option (default
    True = strict). Only relevant on Python 3.13+, where
    ssl.create_default_context() enables VERIFY_X509_STRICT by default; some
    TLS-inspecting proxies issue certs that fail that specific RFC 5280
    profile check even though they are otherwise trustworthy. Setting
    strict_tls=False clears just that flag -- CERT_REQUIRED/check_hostname
    stay on, so this is narrower than verify_ssl=False.
"""

import datetime
import gzip
import hashlib
import hmac
import random
import socket
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

_S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_UNRESERVED = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)

# HTTP statuses worth retrying (transient). 429 = throttle; 5xx = server/LB.
# S3/R2 throttling surfaces as 503 SlowDown and sometimes 500 InternalError.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


# ---------------------------------------------------------------------------
# SigV4 primitives (isolated so they can be unit-tested without a network)
# ---------------------------------------------------------------------------

def uri_encode(value, encode_slash=True):
    """AWS-style percent-encoding. Unreserved chars pass through; everything
    else is %XX (uppercase). '/' is preserved when encode_slash is False."""
    out = []
    for ch in value:
        if ch in _UNRESERVED:
            out.append(ch)
        elif ch == "/" and not encode_slash:
            out.append("/")
        else:
            out.append("".join("%{:02X}".format(b) for b in ch.encode("utf-8")))
    return "".join(out)


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    k = _hmac(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _hmac(k, region)
    k = _hmac(k, service)
    return _hmac(k, "aws4_request")


def build_authorization(access_key, secret_key, region, service, method,
                        canonical_uri, canonical_querystring, headers,
                        payload_hash, amz_date, datestamp):
    """Return (authorization_header, signature_hex) for a SigV4 request.

    `headers` is a dict of headers to sign (lowercased names); it must include
    host, x-amz-content-sha256, and x-amz-date.
    """
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(
        "{}:{}\n".format(k, headers[k]) for k in sorted(headers)
    )
    canonical_request = "\n".join([
        method, canonical_uri, canonical_querystring,
        canonical_headers, signed_headers, payload_hash,
    ])
    scope = "{}/{}/{}/aws4_request".format(datestamp, region, service)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(
        _signing_key(secret_key, datestamp, region, service),
        string_to_sign.encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    authz = (
        "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={}, Signature={}"
        .format(access_key, scope, signed_headers, signature)
    )
    return authz, signature


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def _parse_s3_error_code(body):
    """Extract the <Code> from an S3/R2 error body, or '' if unparseable."""
    try:
        root = ET.fromstring(body)
        code = root.findtext("Code") or root.findtext(_S3_NS + "Code")
        return code or ""
    except ET.ParseError:
        return ""


class R2Error(Exception):
    """Non-2xx or transport failure. Carries HTTP status (0 for transport),
    the parsed S3 error code (e.g. AccessDenied, NoSuchBucket,
    SignatureDoesNotMatch), and the raw body."""

    def __init__(self, status, code, body):
        self.status = status
        self.code = code
        self.body = body
        super().__init__("R2 error status={} code={} body={}".format(
            status, code, (body or "")[:300]))


# ---------------------------------------------------------------------------
# R2 client
# ---------------------------------------------------------------------------

class R2Client(object):

    def __init__(self, account_id, access_key, secret_key, region="auto",
                 verify_ssl=True, ca_bundle=None, timeout=60,
                 max_attempts=5, backoff_base=0.5, backoff_cap=20.0,
                 proxy_url=None, strict_tls=True):
        self.host = "{}.r2.cloudflarestorage.com".format(account_id)
        self.endpoint = "https://" + self.host
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.service = "s3"
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap

        ctx = ssl.create_default_context()
        if ca_bundle:
            ctx.load_verify_locations(ca_bundle)
        if not verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif not strict_tls and hasattr(ssl, "VERIFY_X509_STRICT"):
            # Python 3.13+ turns on VERIFY_X509_STRICT by default in
            # create_default_context(), which enforces RFC 5280 profile
            # checks (e.g. a leaf cert must carry an Authority Key
            # Identifier extension). Some enterprise TLS-inspecting proxies
            # mint re-signed certs that are valid for normal purposes but
            # fail this stricter check, surfacing as
            # "CERTIFICATE_VERIFY_FAILED: Missing Authority Key Identifier".
            # Clearing just this flag keeps full chain + hostname
            # verification (still CERT_REQUIRED / check_hostname=True);
            # it only relaxes the stricter X.509 profile conformance added
            # in 3.13. See github.com/cloudflare/splunk-ta-cloudflare-r2/issues/3
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._ssl_ctx = ctx

        # HTTP proxy support (globalConfig proxyTab; see cloudflare_r2_helper.py).
        # HTTP only -- R2's S3-compatible API is HTTPS-only, so there is no
        # SOCKS4/5 use case here, and adding SOCKS support would require a
        # third-party dependency (PySocks) this client deliberately avoids.
        # Credentials, if any, are embedded in the proxy URL
        # (http://user:pass@host:port) -- the standard stdlib ProxyHandler
        # pattern; no extra library needed.
        # A proxy that terminates/re-signs TLS (a common enterprise "SSL
        # bump" setup, not just a blind CONNECT relay) still works exactly
        # like today's TLS-inspection support: point ca_bundle at the
        # proxy's own CA. The proxy is transport-layer only and has no
        # bearing on which cert ends up being validated.
        handlers = [urllib.request.HTTPSHandler(context=self._ssl_ctx)]
        if proxy_url:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        self._opener = urllib.request.build_opener(*handlers)

    def _sleep_for(self, attempt):
        # exponential backoff with full jitter, capped
        delay = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return random.uniform(0, delay)

    def _build_signed_request(self, method, path, query, body):
        # Python 3.12+ deprecates datetime.utcnow(); use tz-aware UTC.
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        canonical_uri = uri_encode(path, encode_slash=False)
        items = sorted(
            (uri_encode(k), uri_encode(str(v))) for k, v in query.items()
        )
        canonical_qs = "&".join("{}={}".format(k, v) for k, v in items)

        headers = {
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        authz, _ = build_authorization(
            self.access_key, self.secret_key, self.region, self.service,
            method, canonical_uri, canonical_qs, headers, payload_hash,
            amz_date, datestamp,
        )
        url = self.endpoint + canonical_uri
        if canonical_qs:
            url += "?" + canonical_qs
        req = urllib.request.Request(url, data=body if body else None,
                                     method=method)
        req.add_header("Authorization", authz)
        req.add_header("x-amz-content-sha256", payload_hash)
        req.add_header("x-amz-date", amz_date)
        return req

    def _request(self, method, path, query=None, body=b""):
        """Sign, send, and retry transient failures. Returns the urlopen
        response (caller may stream it). Raises R2Error on non-retryable
        errors or after exhausting attempts.

        Retries: HTTP 429/500/502/503/504 and transport errors
        (URLError / socket.timeout). Does NOT retry other 4xx
        (AccessDenied, NoSuchBucket, SignatureDoesNotMatch, etc.)."""
        query = query or {}
        last_exc = None
        for attempt in range(1, self.max_attempts + 1):
            # Re-sign every attempt (x-amz-date must be fresh, else 403 skew).
            req = self._build_signed_request(method, path, query, body)
            try:
                return self._opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                body_txt = exc.read().decode("utf-8", "replace")
                code = _parse_s3_error_code(body_txt)
                if exc.code in _RETRYABLE_STATUS and attempt < self.max_attempts:
                    last_exc = R2Error(exc.code, code, body_txt)
                    time.sleep(self._sleep_for(attempt))
                    continue
                raise R2Error(exc.code, code, body_txt)
            except (urllib.error.URLError, socket.timeout) as exc:
                # DNS/connect/read timeout/TLS handshake — transient; retry.
                last_exc = R2Error(0, "TransportError", str(exc))
                if attempt < self.max_attempts:
                    time.sleep(self._sleep_for(attempt))
                    continue
                raise last_exc
        raise last_exc  # pragma: no cover

    # -- operations used by the TA ------------------------------------------

    def list_objects_v2(self, bucket, prefix=None, start_after=None,
                        continuation_token=None, max_keys=1000):
        """One ListObjectsV2 page. Returns (keys, is_truncated, next_token)."""
        query = {"list-type": "2", "max-keys": max_keys}
        if prefix:
            query["prefix"] = prefix
        if start_after:
            query["start-after"] = start_after
        if continuation_token:
            query["continuation-token"] = continuation_token
        resp = self._request("GET", "/" + bucket, query=query)
        root = ET.fromstring(resp.read().decode("utf-8"))
        keys = []
        for contents in root.findall(_S3_NS + "Contents"):
            key = contents.findtext(_S3_NS + "Key")
            if key and not key.endswith("/"):  # skip directory placeholders
                keys.append(key)
        truncated = (root.findtext(_S3_NS + "IsTruncated") == "true")
        next_token = root.findtext(_S3_NS + "NextContinuationToken")
        return keys, truncated, next_token

    def iter_object_keys(self, bucket, prefix=None, start_after=None):
        """Generator over all object keys, following continuation tokens."""
        token = None
        while True:
            keys, truncated, token = self.list_objects_v2(
                bucket, prefix=prefix, start_after=start_after,
                continuation_token=token,
            )
            for key in keys:
                yield key
            if not truncated:
                return
            start_after = None  # continuation token carries position now

    def iter_object_lines(self, bucket, key):
        """Stream one object, gunzip incrementally, yield non-empty text lines.
        Memory-safe for large Logpush files (no full-object buffering)."""
        resp = self._request("GET", "/{}/{}".format(bucket, key))
        with gzip.GzipFile(fileobj=resp) as gz:
            for raw in gz:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    yield line
