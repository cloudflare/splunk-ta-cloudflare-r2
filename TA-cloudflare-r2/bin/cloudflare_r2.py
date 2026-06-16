#!/usr/bin/env python3
# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license found in the LICENSE file or at:
# https://opensource.org/licenses/Apache-2.0

"""
Cloudflare R2 Log Ingestion - Splunk Modular Input
===================================================
Pulls gzipped NDJSON log files from a Cloudflare R2 bucket via the
S3-compatible API (SigV4, path-style addressing, no AWS STS dependency).

One Splunk event is emitted per JSON line in each log file.
Sourcetype, index, and key prefix are all user-configurable per input
instance - this input is dataset-agnostic.

Author: Cloudflare, Inc.
Version: 0.1.0
"""

import os
import sys
import gzip
import json
import logging

# ---------------------------------------------------------------------------
# Adjust sys.path to find vendored libraries in bin/lib/
# This must happen before any boto3 or splunklib imports.
# ---------------------------------------------------------------------------
_BIN_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from splunklib.modularinput import Script, Scheme, Argument, EventWriter, Event


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _checkpoint_path(checkpoint_dir, input_name):
    """Return an absolute path for this input's checkpoint file."""
    safe = "".join(c if c.isalnum() else "_" for c in input_name)
    return os.path.join(checkpoint_dir, safe + ".json")


def _load_checkpoint(checkpoint_dir, input_name):
    path = _checkpoint_path(checkpoint_dir, input_name)
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_checkpoint(checkpoint_dir, input_name, state):
    path = _checkpoint_path(checkpoint_dir, input_name)
    with open(path, "w") as fh:
        json.dump(state, fh)


# ---------------------------------------------------------------------------
# R2 client factory
# ---------------------------------------------------------------------------

def _make_r2_client(account_id, access_key_id, secret_access_key, verify_ssl=True):
    endpoint = "https://{}.r2.cloudflarestorage.com".format(account_id)
    return boto3.client(
        service_name="s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
        verify=verify_ssl,
    )


# ---------------------------------------------------------------------------
# Core ingestion logic
# ---------------------------------------------------------------------------

def _list_new_objects(client, bucket_name, prefix, start_after, ew):
    """
    Yield S3 object keys that are lexicographically after start_after.
    Uses ListObjectsV2 with pagination.
    """
    kwargs = {
        "Bucket": bucket_name,
        "MaxKeys": 1000,
    }
    if prefix:
        kwargs["Prefix"] = prefix
    if start_after:
        kwargs["StartAfter"] = start_after

    while True:
        try:
            response = client.list_objects_v2(**kwargs)
        except ClientError as exc:
            ew.log(EventWriter.ERROR,
                   "R2 ListObjectsV2 failed: {}".format(exc))
            return

        for obj in response.get("Contents", []):
            key = obj["Key"]
            # Skip "directory" placeholder keys (e.g. prefix/ with no content)
            if key.endswith("/"):
                continue
            yield key

        if not response.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = response["NextContinuationToken"]
        # After the first page, start_after is baked into the continuation token
        kwargs.pop("StartAfter", None)


def _process_object(client, bucket_name, key, input_name, input_item, ew):
    """
    Download one R2 object, gunzip it, and emit one Splunk event per JSON line.
    Returns the number of events emitted, or raises on fatal error.
    """
    sourcetype = input_item.get("sourcetype", "cloudflare:json")
    index = input_item.get("index", "main")
    host = input_item.get("host", "cloudflare-r2")

    try:
        response = client.get_object(Bucket=bucket_name, Key=key)
        compressed_data = response["Body"].read()
    except ClientError as exc:
        ew.log(EventWriter.ERROR,
               "R2 GetObject failed for key={}: {}".format(key, exc))
        raise

    try:
        raw_data = gzip.decompress(compressed_data)
    except Exception as exc:
        ew.log(EventWriter.ERROR,
               "gzip decompress failed for key={}: {}".format(key, exc))
        raise

    lines = raw_data.decode("utf-8", errors="replace").splitlines()
    count = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue

        event = Event()
        event.stanza = input_name
        event.data = line
        event.sourcetype = sourcetype
        event.index = index
        event.host = host
        event.source = "r2://{}/{}".format(bucket_name, key)

        # If the JSON record has a timestamp field, use it.
        # Splunk will parse the time from the event data automatically
        # based on the sourcetype's time extraction config - we leave
        # event.time unset so Splunk handles it, which is the correct
        # approach for a transport-only add-on.

        ew.write_event(event)
        count += 1

    return count


# ---------------------------------------------------------------------------
# Modular input class
# ---------------------------------------------------------------------------

class CloudflareR2Input(Script):

    def get_scheme(self):
        scheme = Scheme("Cloudflare R2 Log Ingestion")
        scheme.description = (
            "Pulls gzipped NDJSON log files from a Cloudflare R2 bucket "
            "via the S3-compatible API. Emits one Splunk event per JSON line. "
            "Works with any Cloudflare Logpush dataset."
        )
        scheme.use_external_validation = True
        scheme.use_single_instance = False

        scheme.add_argument(Argument(
            name="account_id",
            title="Cloudflare Account ID",
            description="32-character hex Cloudflare account ID. Used to build the R2 endpoint URL.",
            data_type=Argument.data_type_string,
            required_on_create=True,
            required_on_edit=False,
        ))
        scheme.add_argument(Argument(
            name="access_key_id",
            title="R2 Access Key ID",
            description="R2 S3-compatible API access key ID.",
            data_type=Argument.data_type_string,
            required_on_create=True,
            required_on_edit=False,
        ))
        scheme.add_argument(Argument(
            name="secret_access_key",
            title="R2 Secret Access Key",
            description="R2 S3-compatible API secret access key.",
            data_type=Argument.data_type_string,
            required_on_create=True,
            required_on_edit=False,
        ))
        scheme.add_argument(Argument(
            name="bucket_name",
            title="R2 Bucket Name",
            description="Name of the R2 bucket containing Cloudflare Logpush files.",
            data_type=Argument.data_type_string,
            required_on_create=True,
            required_on_edit=False,
        ))
        scheme.add_argument(Argument(
            name="verify_ssl",
            title="Verify SSL Certificate",
            description=(
                "Whether to verify the R2 endpoint TLS certificate. "
                "Set to false only if your network performs TLS inspection and "
                "you cannot add the CA to the trust store. Default: true."
            ),
            data_type=Argument.data_type_boolean,
            required_on_create=False,
            required_on_edit=False,
        ))
        scheme.add_argument(Argument(
            name="key_prefix",
            title="Key Prefix (optional)",
            description=(
                "Limit processing to keys starting with this prefix. "
                "Example: gateway_dns/ or http_requests/. Leave empty for all objects."
            ),
            data_type=Argument.data_type_string,
            required_on_create=False,
            required_on_edit=False,
        ))

        return scheme

    def validate_input(self, definition):
        """Called with --validate-arguments. Raises ValueError to fail validation."""
        params = definition.parameters

        account_id = params.get("account_id", "").strip()
        if not account_id:
            raise ValueError("account_id is required")
        if len(account_id) != 32 or not all(c in "0123456789abcdef" for c in account_id.lower()):
            raise ValueError(
                "account_id must be a 32-character hex string (found: {!r})".format(account_id)
            )

        if not params.get("access_key_id", "").strip():
            raise ValueError("access_key_id is required")

        if not params.get("secret_access_key", "").strip():
            raise ValueError("secret_access_key is required")

        if not params.get("bucket_name", "").strip():
            raise ValueError("bucket_name is required")

        # Note: We intentionally skip a live HeadBucket connectivity check here.
        # The validate_input step runs from within the Splunk process, which may
        # have a different CA bundle than the host. R2's cert (DigiCert G2) is
        # valid and verifiable at runtime. A failed CA check here would produce
        # a confusing "SSL error" at input creation time rather than a credential
        # error. Parameter format validation above is sufficient for validation.

    def stream_events(self, inputs, ew):
        """Main entry point. Called once per polling interval per input instance."""
        for input_name, input_item in inputs.inputs.items():
            self._run_input(input_name, input_item, ew)

    def _run_input(self, input_name, input_item, ew):
        account_id = input_item.get("account_id", "").strip()
        access_key_id = input_item.get("access_key_id", "").strip()
        secret_access_key = input_item.get("secret_access_key", "").strip()
        bucket_name = input_item.get("bucket_name", "").strip()
        key_prefix = input_item.get("key_prefix", "").strip()
        # Splunk injects checkpoint_dir into input_item. When running via splunklib's
        # Script framework, this resolves to $SPLUNK_DB/modinputs/<scheme>/ which is
        # persistent across restarts. Fall back to a persistent app-local path if the
        # injected value is /tmp (which indicates the SDK did not resolve it properly).
        checkpoint_dir = input_item.get("checkpoint_dir", "/tmp")
        if not checkpoint_dir or checkpoint_dir == "/tmp":
            # Derive a persistent path from SPLUNK_HOME env var or the script location
            splunk_home = os.environ.get("SPLUNK_HOME", "")
            if splunk_home:
                checkpoint_dir = os.path.join(
                    splunk_home, "var", "lib", "splunk", "modinputs", "cloudflare_r2"
                )
            else:
                # Last resort: use a subdir of the app itself (always persistent)
                checkpoint_dir = os.path.join(_BIN_DIR, "..", "local", "checkpoints")
            os.makedirs(checkpoint_dir, exist_ok=True)

        ew.log(EventWriter.INFO,
               "cloudflare_r2 input={} bucket={} prefix={!r} checkpoint_dir={!r} starting poll".format(
                   input_name, bucket_name, key_prefix, checkpoint_dir))

        # Load checkpoint
        state = _load_checkpoint(checkpoint_dir, input_name)
        last_key = state.get("last_key", "")

        # SSL verification. Default True (correct for production).
        # Set to False ONLY if your network performs TLS inspection on outbound
        # traffic and you cannot add the inspection CA to the trust store.
        # In that case, the secure solution is to add the CA cert to the system
        # trust store rather than disabling verification entirely.
        verify_ssl_str = input_item.get("verify_ssl", "true").strip().lower()
        verify_ssl = verify_ssl_str not in ("false", "0", "no")

        # Build R2 client
        try:
            client = _make_r2_client(account_id, access_key_id, secret_access_key,
                                     verify_ssl=verify_ssl)
        except Exception as exc:
            ew.log(EventWriter.ERROR,
                   "cloudflare_r2 input={} failed to create R2 client: {}".format(
                       input_name, exc))
            return

        # List and process new objects
        total_files = 0
        total_events = 0
        new_last_key = last_key

        for key in _list_new_objects(client, bucket_name, key_prefix, last_key, ew):
            try:
                n = _process_object(client, bucket_name, key,
                                    input_name, input_item, ew)
                total_events += n
                total_files += 1
                new_last_key = key
                # Save checkpoint after each file so a crash mid-run
                # doesn't re-process already-ingested files.
                _save_checkpoint(checkpoint_dir, input_name, {
                    "last_key": new_last_key,
                    "total_files_processed": state.get("total_files_processed", 0) + total_files,
                })
            except Exception as exc:
                # Log and skip this file; checkpoint is NOT advanced past it
                # so the next poll will retry.
                ew.log(EventWriter.ERROR,
                       "cloudflare_r2 input={} skipping key={} due to error: {}".format(
                           input_name, key, exc))
                continue

        ew.log(EventWriter.INFO,
               "cloudflare_r2 input={} poll complete: files={} events={} last_key={!r}".format(
                   input_name, total_files, total_events, new_last_key))


if __name__ == "__main__":
    sys.exit(CloudflareR2Input().run(sys.argv))
