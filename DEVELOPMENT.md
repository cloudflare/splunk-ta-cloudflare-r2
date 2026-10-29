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
  │     ├── resolve proxy settings # app-wide [proxy] stanza, ta_..._settings.conf (optional; fails open)
  │     ├── list new keys          # dedupe against KV Store processed-key set (+ lookback)
  │     ├── process object         # streaming gunzip → emit one event per NDJSON line
  │     └── checkpoint             # upsert processed key in KV Store (prune by lookback)
  │
  ├── bin/r2client.py              # stdlib-only R2 client — NO third-party deps
  │     ├── SigV4 signer           # hmac + hashlib (offline known-answer tested)
  │     ├── HTTPS transport        # urllib.request over stdlib ssl (retry + backoff)
  │     ├── proxy support          # optional urllib.request.ProxyHandler; HTTP only, Basic auth only
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

### Splunk Cloud support

Not currently pursued. A Splunk Cloud developer account (required for Cloud vetting) has
been requested and is pending admission to Splunk's beta program; Cloud support can be
revisited once that access is available. Until then, this add-on targets on-premises
Splunk Enterprise only. Inputs require KV Store, so they must run on a heavy forwarder /
IDM / standalone, **not a Universal Forwarder**. Minimum supported Splunk is **9.4**.

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
ucc-gen build --ta-version 1.0.1
ucc-gen package --path output/TA_cloudflare_r2
```

`ucc-gen build` installs the Splunk-supplied libraries into
`output/TA_cloudflare_r2/lib/` and injects `python.version` / `python.required`.
`ucc-gen package` emits `TA_cloudflare_r2-1.0.1.spl`.

### Run AppInspect

```bash
pip install --upgrade splunk-appinspect
splunk-appinspect inspect TA_cloudflare_r2-1.0.1.spl --mode precert
```

**Always upgrade `splunk-appinspect` before a release check, including in any
cached/local Docker image.** Splunkbase runs new checks against the latest
published version as soon as it's released, and a stale local copy can pass
cleanly while the real Splunkbase submission catches something new (observed
in practice at least twice: local precert green, submission caught an issue
the cached version's checks didn't have yet). If using a local image, rebuild
it rather than reusing an old one:

```bash
docker build -t cf-appinspect:local -f - . <<'DOCKERFILE'
FROM python:3.9-slim
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir splunk-appinspect
ENTRYPOINT ["splunk-appinspect"]
DOCKERFILE
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

The three direct requirements pinned in `package/lib/requirements.txt` are
`splunktaucclib`, `splunk-sdk` (`splunklib`), and `solnlib`, plus `urllib3 < 2` (pinned
directly - see note below). `ucc-gen build` resolves their transitive dependencies too,
so `lib/` ends up with 14 packages total:

| Package | Version | License |
|---|---|---|
| splunk-sdk (`splunklib`) | 2.1.1 | Apache-2.0 |
| solnlib | 5.4.0 | Apache-2.0 |
| splunktaucclib | 6.5.3 | Apache-2.0 |
| splunktalib | 3.0.6 | Apache-2.0 |
| requests | 2.32.5 | Apache-2.0 |
| deprecation | 2.1.0 | Apache-2.0 |
| sortedcontainers | 2.4.0 | Apache-2.0 |
| urllib3 | 1.26.20 | MIT |
| charset-normalizer | 3.4.7 | MIT |
| idna | 3.18 | BSD-3-Clause |
| PySocks | 1.7.1 | BSD |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| certifi | 2026.6.17 | MPL-2.0 |
| defusedxml | 0.7.1 | PSF-2.0 |

Only `splunk-sdk`, `solnlib`, and `splunktaucclib` are imported directly by this add-on's
code; the rest are transitive dependencies pulled in by those three (or by `urllib3`
itself). Full license texts for all 14, sourced verbatim from each package's own bundled
license file, are in `package/THIRD-PARTY-NOTICES.txt` (ships as
`TA_cloudflare_r2/THIRD-PARTY-NOTICES.txt`).

Note: `urllib3` ships in `lib/` pinned `< 2` for the Splunk 9.4 / Python 3.9 / OpenSSL
baseline (urllib3 2.x refuses OpenSSL <1.1.1, and Splunk 9.4 ships OpenSSL 1.0.2), not
because `r2client.py` uses it - `r2client.py` imports no third-party package at all. Not
shipping the boto3 stack still removes the EOL-SDK liability (boto3 1.43+ requires Python
≥3.10; Splunk 9.4/10.0 ship 3.9).

To regenerate `THIRD-PARTY-NOTICES.txt` after a dependency bump, rebuild first so `lib/`
reflects the new resolution, then re-run the generator against it:

```bash
ucc-gen build --source package --ta-version <version>
python3 tools/generate_third_party_notices.py \
  --lib output/TA_cloudflare_r2/lib \
  --out package/THIRD-PARTY-NOTICES.txt
```

The script reads each package's actual installed `*.dist-info/METADATA` and bundled
license file rather than a hand-maintained list, and prints a warning for any package
missing a license file (none currently) so that case can't silently go unnoticed.
