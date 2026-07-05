# Development Guide

This document covers the technical design, architecture decisions, and known gaps
for contributors looking to improve or productionize this add-on.

---

## Architecture

> **Note:** The add-on runs on the Splunk UCC framework. Its R2 client
> (`r2client.py`) is pure standard library - no boto3, botocore, or urllib3.
> The sections below describe that architecture.

```
Splunk UCC add-on (cloudflare_r2 input)
  │
  ├── bin/cloudflare_r2_helper.py  # inputHelperModule: top-level validate_input + stream_events
  │     ├── resolve account        # read secret from storage/passwords via solnlib
  │     ├── list new keys          # dedupe against KV Store processed-key set (+ lookback)
  │     ├── process object         # streaming gunzip → emit one event per NDJSON line
  │     └── checkpoint             # upsert processed key in KV Store (prune by lookback)
  │
  ├── bin/r2client.py              # stdlib-only R2 client — NO third-party deps
  │     ├── SigV4 signer           # hmac + hashlib (offline known-answer tested)
  │     ├── HTTPS transport        # urllib.request over stdlib ssl (retry + backoff)
  │     ├── list_objects_v2        # xml.etree parse, continuation-token pagination
  │     └── iter_object_lines      # gzip.GzipFile(fileobj=...) streaming, memory-safe
  │
  └── lib/                         # splunklib / solnlib / splunktaucclib (supplied by ucc-gen)

tests/test_sigv4.py                # offline SigV4 known-answer test (CI/AppInspect); lives
                                    # outside package/ so it is not shipped in the .spl
```

There is **no vendored boto3 stack** — the R2 access layer is entirely standard library.

---

## Why no AWS STS dependency

The [Splunk Add-on for AWS](https://splunkbase.splunk.com/app/1876) calls
`sts.get_caller_identity()` to validate credentials before saving them and again
at input runtime. Cloudflare R2 has no STS service, so the AWS TA fails regardless
of how it is configured (`host_name`, `s3_private_endpoint_url`, `sts_private_endpoint_url`
- all confirmed dead-ends against v8.1.2).

This add-on bypasses STS entirely with a small **standard-library** SigV4 client
(`bin/r2client.py`, no boto3):
- endpoint `https://<account_id>.r2.cloudflarestorage.com`
- `region = "auto"`, `service = "s3"`, **path-style** URLs
- SigV4 signing via `hmac`/`hashlib`; HTTPS via `urllib.request` over stdlib `ssl`
- Static R2 access key / secret (S3 API token); the secret is read from
  `storage/passwords` at runtime, never from the stanza

Only two S3 operations are used (`ListObjectsV2`, `GetObject`), so boto3/botocore would
be enormous overkill; the stdlib client also avoids shipping an EOL-pinned SDK (boto3
1.43+ dropped Python 3.9, which Splunk 9.4/10.0 ship). The SigV4 signer is guarded by an
offline known-answer test against AWS's official get-vanilla vector.

---

## Checkpointing design

The add-on tracks progress in a **typed KV Store collection**
(`TA_cloudflare_r2_processed_keys`), one row per ingested object:

    _key  = "<input_name>|<object_key>"      # e.g. "prod_dns|gateway_dns/20260608/…_abc123.log.gz"
    input = "<input_name>"                    # string
    ts    = <object LastModified, epoch sec>  # number (enforced)

The collection is defined with `enforceTypes = true` and `field.ts = number` so that
range pruning compares `ts` numerically, not lexicographically.

On each poll the input: (1) lists the bucket/prefix for the configured **lookback
window** (`lookback_days`); (2) skips any object whose `<input>|<key>` row already
exists (per-input dedupe); (3) streams and emits new objects, then **upserts** their
rows; (4) **prunes** rows older than the lookback window (delete by `ts < cutoff`, with
a safety margin) to bound collection growth.

**Why a processed-key set, not a single cursor.** Logpush filenames are timestamped by
the log *time window*, not by write/delivery time, and Logpush retries delivery. A
single monotonic `StartAfter` cursor would permanently skip a late-delivered batch whose
key sorts before the cursor (silent data loss). Re-listing the lookback window and
deduping per object avoids that.

**At-least-once delivery.** A row is written only *after* its events are emitted, so an
ungraceful crash mid-object may re-ingest that object on the next poll. Under normal
operation each object is ingested exactly once and no search-time dedup is required; to
dedup after a crash, use `RayID` (HTTP Requests) or `QueryID` (Gateway DNS).

**Write-once source.** Logpush never modifies an existing object, so there is no
byte-offset tracking within a file.

---

## Known gaps for a production deployment

### Splunk Cloud compatibility

Targeting Splunk Cloud (Victoria) + Cloud vetting via UCC. The stdlib client and the KV
Store checkpoint (no app-local filesystem writes) are chosen partly for Cloud rules. Note:
inputs require KV Store, so they must run on a heavy forwarder / IDM / standalone, **not a
Universal Forwarder**. Minimum supported Splunk is **9.4**.

<!-- Credential encryption and TLS verify_ssl/ca_bundle used to be listed here as
     "known gaps" - both are resolved design decisions, not gaps, and duplicating
     them under this heading was confusing. See the Architecture section above and
     SECURITY.md for credential storage; see README.md's "TLS inspection and CA
     bundles" section for the verify_ssl/ca_bundle guidance. -->

---

## Local development setup

### Prerequisites

- Docker (OrbStack, Colima, or Docker Desktop)
- Python 3.9+ (for `ucc-gen`; the add-on runtime uses Splunk's bundled Python)
- No third-party Python packages are required to run the R2 client (standard library only)

### Run Splunk locally

```bash
docker run -d \
  --name splunk-dev \
  -p 8000:8000 -p 8089:8089 \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
  -e SPLUNK_PASSWORD='<your-admin-password>' \
  -v $(pwd)/output/TA_cloudflare_r2:/opt/splunk/etc/apps/TA_cloudflare_r2 \
  splunk/splunk:9.4
```

**Note**: `splunk/splunk` is `linux/amd64` only. On Apple Silicon, Docker runs it
under Rosetta 2 emulation. First boot takes 3-5 minutes.

### Seed test data

```bash
python3 tools/seed_test_data.py \
  --account-id <your-account-id> \
  --access-key <your-r2-access-key-id> \
  --secret-key <your-r2-secret-access-key> \
  --bucket <your-bucket-name> \
  --count 3
```

This generates schema-accurate synthetic Logpush files (Gateway DNS and HTTP
Requests) and uploads them to your R2 bucket.

### Rebuild the package

The add-on is generated from `package/` + `globalConfig.json` by `ucc-gen`. Build
the app into `output/`, then package a versioned `.spl`:

```bash
ucc-gen build --ta-version 1.0.0
ucc-gen package --path output/TA_cloudflare_r2
```

`ucc-gen build` installs the Splunk-supplied libraries into
`output/TA_cloudflare_r2/lib/` and injects `python.version` / `python.required`.
`ucc-gen package` emits `TA_cloudflare_r2-1.0.0.spl`.

### Run AppInspect

```bash
pip install splunk-appinspect
splunk-appinspect inspect TA_cloudflare_r2-1.0.0.spl --mode precert
```

---

## Dependencies

The R2 access layer (`bin/r2client.py`) imports the **Python standard library only** — it
pulls in no boto3, botocore, s3transfer, jmespath, or python-dateutil, and no urllib3.
This is a deliberate design choice:

- SigV4 signing → `hmac` + `hashlib`
- HTTPS transport → `urllib.request` / `http.client` over stdlib `ssl`
- ListObjectsV2 XML → `xml.etree.ElementTree`
- Streaming decompression → `gzip`

The runtime libraries in `lib/` are the Splunk-supplied ones that `ucc-gen build`
installs:

| Package | License | Notes |
|---|---|---|
| splunk-sdk (`splunklib`) | Apache 2.0 | modularinput framework |
| solnlib | Apache 2.0 | credentials (`storage/passwords`), KV Store checkpoint |
| splunktaucclib | Apache 2.0 | UCC REST handler / config layer |
| urllib3 | MIT | transitive dependency of splunk-sdk / solnlib; pinned `< 2` |

Note: `urllib3` ships in `lib/` only as a transitive dependency of the Splunk-supplied
libraries above (pinned `< 2` for the Splunk 9.4 / Python 3.9 / OpenSSL baseline), not
because `r2client.py` uses it. Not shipping the boto3 stack still removes the EOL-SDK
liability (boto3 1.43+ requires Python ≥3.10; Splunk 9.4/10.0 ship 3.9). Run a license
sweep of the final `lib/` and emit a `THIRD-PARTY-NOTICES` manifest before publishing.
