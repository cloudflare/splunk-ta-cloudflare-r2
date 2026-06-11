# Development Guide

This document covers the technical design, architecture decisions, and known gaps
for contributors looking to improve or productionize this add-on.

---

## Architecture

```
Splunk modular input (cloudflare_r2://)
  │
  ├── bin/cloudflare_r2.py         # Core input logic
  │     ├── _make_r2_client()      # boto3 S3 client configured for R2
  │     ├── _list_new_objects()    # ListObjectsV2 with StartAfter checkpointing
  │     ├── _process_object()      # GetObject + gunzip + emit one event per line
  │     ├── _load_checkpoint()     # Read last_key from JSON checkpoint file
  │     └── _save_checkpoint()     # Write last_key after each file processed
  │
  └── bin/lib/                     # Vendored Python dependencies
        ├── boto3/                 # AWS SDK (S3 client only)
        ├── botocore/              # boto3 dependency (S3 + STS service defs only)
        ├── splunklib/             # Splunk Python SDK (modularinput framework)
        └── urllib3/               # HTTP client
```

---

## Why no AWS STS dependency

The [Splunk Add-on for AWS](https://splunkbase.splunk.com/app/1876) calls
`sts.get_caller_identity()` to validate credentials before saving them and again
at input runtime. Cloudflare R2 has no STS service, so the AWS TA fails regardless
of how it is configured (`host_name`, `s3_private_endpoint_url`, `sts_private_endpoint_url`
- all confirmed dead-ends against v8.1.2).

This add-on bypasses STS entirely by using boto3 directly with:
- `endpoint_url = https://<account_id>.r2.cloudflarestorage.com`
- `region_name = "auto"`
- `config = Config(s3={"addressing_style": "path"})`
- Static `aws_access_key_id` / `aws_secret_access_key` (R2 S3 API tokens)

---

## Checkpointing design

After processing each `.log.gz` file, the input writes a checkpoint file:

```
$SPLUNK_HOME/var/lib/splunk/modinputs/cloudflare_r2/<stanza_name>.json
{"last_key": "gateway_dns/20260608/20260608T120000Z_..._abc123.log.gz", "total_files_processed": 42}
```

On the next poll, `ListObjectsV2` is called with `StartAfter=last_key`, returning
only keys lexicographically after the checkpoint. Since Logpush key names are
timestamp-prefixed, this reliably returns only new files.

**Why this works for Logpush**: Logpush is write-once. Each batch creates a new
file with a new key and never modifies existing files. There is no need for
byte-offset tracking within files.

**At-least-once delivery**: If Splunk crashes mid-file before the checkpoint saves,
that file is re-processed on restart. Deduplicate at search time using `RayID`
(HTTP requests) or `QueryID` (Gateway DNS) if needed.

---

## Known gaps for a production deployment

### 1. Credential encryption (highest priority)

Currently, `secret_access_key` is stored in `inputs.conf` in plaintext. The
`type="password"` widget in the manager XML renders masked in the UI but the
modular input EAI handler does not automatically route it to `storage/passwords`.

**Fix**: Implement a custom REST handler that calls
`POST /services/storage/passwords` on input create/edit, and update the modular
input to read credentials via `self.service.storage_passwords`. This is the
standard pattern used by production Splunk TAs.

### 2. Splunk Cloud compatibility

Tested on Splunk Enterprise 10.4.0 only. Splunk Cloud (Victoria experience) has
different path conventions and stricter AppInspect checks. Specifically:
- `default/inputs.conf` is not synced to indexers in Victoria
- The `checkpoint_dir` fallback logic should be reviewed for Splunk Cloud

### 3. `verify_ssl` parameter

The `verify_ssl = false` option exists for environments where outbound TLS
inspection rewrites the R2 endpoint's certificate. The correct long-term fix for
affected customers is to add the inspection CA certificate to the Splunk server's
trust store. A `ca_bundle` parameter pointing to a custom CA bundle path would be
a cleaner solution than a boolean disable flag.

---

## Local development setup

### Prerequisites

- Docker (OrbStack, Colima, or Docker Desktop)
- Python 3.10+
- `pip install boto3`

### Run Splunk locally

```bash
docker run -d \
  --name splunk-dev \
  -p 8000:8000 -p 8089:8089 \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
  -e SPLUNK_PASSWORD=changeme1! \
  -v $(pwd)/TA-cloudflare-r2:/opt/splunk/etc/apps/TA-cloudflare-r2 \
  splunk/splunk:latest
```

**Note**: `splunk/splunk` is `linux/amd64` only. On Apple Silicon, Docker runs it
under Rosetta 2 emulation. First boot takes 3-5 minutes.

### Seed test data

```bash
python3 seed_test_data.py \
  --account-id <your-account-id> \
  --access-key <your-r2-access-key-id> \
  --secret-key <your-r2-secret-access-key> \
  --bucket <your-bucket-name> \
  --count 3
```

This generates schema-accurate synthetic Logpush files (Gateway DNS and HTTP
Requests) and uploads them to your R2 bucket.

### Rebuild the package

```bash
COPYFILE_DISABLE=1 tar -czf TA-cloudflare-r2-0.1.0.tgz \
  --exclude="TA-cloudflare-r2/metadata/local.meta" \
  --exclude="*/__pycache__" \
  --exclude="*/.DS_Store" \
  TA-cloudflare-r2/
```

### Run AppInspect

```bash
pip install splunk-appinspect
splunk-appinspect inspect TA-cloudflare-r2-0.1.0.tgz --mode precert
```

---

## Vendored dependencies

All Python dependencies are vendored in `bin/lib/` for Splunk compatibility
(Splunk's Python environment cannot access the host's site-packages).

| Package | Version | License | Notes |
|---|---|---|---|
| boto3 | 1.42.97 | Apache 2.0 | S3 client |
| botocore | 1.42.97 | Apache 2.0 | boto3 core; pruned to S3+STS service defs only |
| s3transfer | 0.16.1 | Apache 2.0 | boto3 transfer manager |
| jmespath | 1.1.0 | MIT | boto3 dependency |
| python-dateutil | 2.9.0 | Apache 2.0 | boto3 dependency |
| urllib3 | 1.26.20 | MIT | HTTP client (1.26.x for Python 3.9 compat) |
| six | 1.17.0 | MIT | Python 2/3 compat shim |
| splunk-sdk | 2.1.1 | Apache 2.0 | Splunk modularinput framework |

To update a dependency, reinstall into `bin/lib/` and re-run AppInspect to
verify no new failures.
