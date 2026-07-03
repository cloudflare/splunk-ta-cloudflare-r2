# Cloudflare R2 Log Ingestion Add-on for Splunk

A Splunk Technology Add-on (TA) that ingests Cloudflare Logpush log files from
Cloudflare R2 via a Python modular input using the S3-compatible API.

**Tested on**: Splunk Enterprise 9.4 (Python 3.9)

---

## Why this exists

The [Splunk Add-on for AWS](https://splunkbase.splunk.com/app/1876) does not work
with Cloudflare R2. It calls `sts.get_caller_identity()` to validate credentials
before saving them. R2 is S3-compatible but has no AWS STS service, so the add-on
rejects R2 credentials unconditionally. This is a confirmed dead-end as of v8.1.2
regardless of configuration - including the Generic S3 input with custom `host_name`.

This add-on solves the gap with a purpose-built modular input that uses SigV4 auth
and path-style S3 calls directly - no STS dependency.

---

## What it does

```
Cloudflare R2 bucket  →  ListObjectsV2 + GetObject  →  gunzip  →  Splunk index
(Logpush NDJSON files)    (SigV4, path-style)          (one event per JSON line)
```

- Works with **any Cloudflare Logpush dataset** (Gateway DNS, HTTP Requests,
  Access, Audit, etc.) - fully dataset-agnostic
- One input instance per bucket/prefix, each with its own sourcetype and index
- Checkpointing via a typed KV Store processed-key set (one entry per ingested
  object, scoped per input) plus a configurable lookback window - each poll
  re-lists the lookback window and skips objects already recorded, so only new
  objects are ingested. Checkpoints survive Splunk restarts, and because dedupe
  is per-object (not a single monotonic cursor), a late-delivered Logpush batch
  whose key sorts before earlier objects is still picked up. Delivery is
  at-least-once: an object's checkpoint is written only after its events are
  emitted, so an ungraceful crash mid-object may re-ingest that object on the
  next poll (a small number of duplicate events). Under normal operation each
  object is ingested exactly once, with no search-time deduplication required.
- Multiple Cloudflare accounts: credentials live in named **Accounts**; each
  input selects an account, so several Cloudflare accounts can feed one Splunk
  instance

---

## Installation

1. Download `TA_cloudflare_r2-1.0.0.spl` from [Releases](../../releases)
2. In Splunk Web: **Apps > Manage Apps > Install app from file**
3. Upload the `.spl` file and click **Upload**

---

## Configuration

This add-on uses the Splunk UCC configuration model: credentials live in a
reusable **Account**, and each **Input** selects an account by name.

### Step 1 — Create an Account

In the add-on's UI, open **Configuration > Cloudflare R2 Account > Add**.

| Field | Description |
|---|---|
| Account name | Unique name for this credential set (referenced by inputs) |
| Cloudflare Account ID | 32-character hex string from your Cloudflare dashboard URL |
| R2 Access Key ID | Generate via: Cloudflare Dashboard > R2 > Manage R2 API Tokens |
| R2 Secret Access Key | Shown once at token creation. Stored encrypted via `storage/passwords`. |
| Verify SSL certificate | Keep enabled (default). For TLS-inspection networks, keep it on and set the CA bundle path below rather than disabling it. Unchecking disables certificate validation entirely (insecure). |
| CA bundle path (on-prem only) | Absolute path, on the Splunk host, to your inspection proxy's root CA (PEM). Use when outbound traffic is TLS-inspected; preferred over disabling verification. Not usable on Splunk Cloud (locked filesystem). See [TLS inspection and CA bundles](#tls-inspection-and-ca-bundles). |

The R2 API token needs **Object Read** permission on the target bucket.

### Step 2 — Create an Input

Open the **Inputs** page and add a **Cloudflare R2 Log Ingestion** input.

| Field | Description |
|---|---|
| Name | Unique name for this input instance |
| Cloudflare R2 account | The Account created in Step 1 |
| R2 bucket name | Name of the R2 bucket containing Logpush files |
| Key prefix (optional) | Subfolder to scope to one dataset (e.g. `gateway_dns/`). Leave blank for all files. |
| Lookback window (days) | How far back each poll re-lists objects to catch late-delivered Logpush batches. Default: 1 |
| Interval | How often to check for new files, in seconds. Default: 300 |
| Source type | Splunk sourcetype for events (see table below) |
| Index | Splunk index to store events |

### Sourcetype mapping

For use with the [Cloudflare App for Splunk](https://splunkbase.splunk.com/app/4501)
(which provides dashboards and field extractions for Cloudflare log data):

| Logpush Dataset | Key Prefix | Sourcetype |
|---|---|---|
| Zero Trust Gateway DNS | `gateway_dns/` | `cloudflare:dns` |
| Zero Trust Gateway HTTP | `gateway_http/` | `cloudflare:http` |
| Zero Trust Gateway Network | `gateway_network/` | `cloudflare:network` |
| Zone HTTP Requests | `http_requests/` | `cloudflare:json` |
| Zero Trust Access | `access/` | `cloudflare:access` |
| Cloudflare Audit Logs | `audit/` | `cloudflare:audit` |

### Multiple datasets from one bucket

Create one input per dataset, each pointing at a different prefix:

```ini
[cloudflare_r2://my_gateway_dns]
account = my_cf_account
bucket_name = my-logpush-bucket
key_prefix = gateway_dns/
sourcetype = cloudflare:dns
index = cloudflare_logs

[cloudflare_r2://my_http_requests]
account = my_cf_account
bucket_name = my-logpush-bucket
key_prefix = http_requests/
sourcetype = cloudflare:json
index = cloudflare_logs
```

### Multiple Cloudflare accounts

Create one Account per Cloudflare account, then point each input at the
appropriate account. No credentials appear in `inputs.conf` — the input only
references an account by name:

```ini
[cloudflare_r2://account_a_dns]
account = cf_account_a
bucket_name = account-a-logs
key_prefix = gateway_dns/

[cloudflare_r2://account_b_dns]
account = cf_account_b
bucket_name = account-b-logs
key_prefix = gateway_dns/
```

---

## TLS inspection and CA bundles

By default the add-on verifies R2's TLS certificate against the system trust
store and needs no extra configuration — R2 endpoints present publicly trusted
certificates.

If the Splunk host's outbound traffic passes through a **TLS-inspection proxy**
(an enterprise "SSL inspection" gateway — for example a corporate Zero Trust
gateway, Zscaler, Netskope, or a firewall doing SSL decryption), that proxy
re-signs connections with its own root CA. Because that root is not in the
default trust store, polls fail with:

```
[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
self signed certificate in certificate chain
```

To fix this **without** weakening security:

1. Obtain your proxy's **root CA certificate** in PEM format from your security
   or network team — the same root your organization already distributes to
   browsers and operating systems for inspection.
2. Place it on the Splunk host that runs the input (search head / heavy
   forwarder / IDM), readable by the Splunk user, e.g.
   `$SPLUNK_HOME/etc/auth/r2_inspection_ca.pem`.
3. In the Account, keep **Verify SSL certificate** enabled and set **CA bundle
   path** to that absolute path.

Notes:

- The CA bundle is **specific to your environment**. This add-on does not ship
  one, and another organization's CA will not work for you.
- **Splunk Cloud** has a locked filesystem, so a CA bundle path cannot be used
  there. Splunk Cloud egress is not typically TLS-inspected, so the default
  verification just works.
- Unchecking **Verify SSL certificate** disables certificate validation
  entirely (insecure) and should only be a temporary diagnostic step.

---

## How Logpush + R2 works

1. Configure a [Cloudflare Logpush job](https://developers.cloudflare.com/logs/logpush/)
   to write to an R2 bucket (the automatic R2 setup in the dashboard is the easiest path)
2. Logpush writes gzipped NDJSON files roughly every 30 seconds:
   `gateway_dns/20260608/20260608T120000Z_20260608T120030Z_abc123.log.gz`
3. This add-on polls R2 on your configured interval, downloads new files,
   decompresses them, and sends each JSON line as a Splunk event

---

## Resetting checkpoints (re-ingest the lookback window)

Checkpoints live in the KV Store collection `TA_cloudflare_r2_processed_keys`
(not in `inputs.conf` and not on the app-local filesystem, so
`splunk clean inputdata` does **not** clear them). Deleting the collection's
data makes the next poll treat every object in the lookback window as new and
re-ingest it:

```bash
curl -k -u admin:'<your-admin-password>' -X DELETE \
  https://localhost:8089/servicesNS/nobody/TA_cloudflare_r2/storage/collections/data/TA_cloudflare_r2_processed_keys
```

The next poll re-ingests only objects within the current **Lookback window
(days)** — objects older than that window fall below the add-on's lookback
floor and are not re-listed. Any objects in the window that were already
indexed are ingested again, producing duplicate events for that window, so
reset only when you intend to backfill.

---

## Requirements

- Splunk Enterprise 9.4 or higher — 9.4 is the oldest actively-supported release and matches a supported enterprise production baseline. Earlier releases (8.x / 9.0–9.3) are past end-of-support and out of scope.
- The R2 client (`bin/r2client.py`) uses the Python standard library only; the Splunk-supplied libraries (splunklib, solnlib, splunktaucclib) are bundled by the build
- Runs on a heavy forwarder / IDM / standalone / search head (requires KV Store; not a Universal Forwarder)
- Cloudflare account with R2 enabled
- R2 API token with **Object Read** permission on the target bucket

---

## Known limitations

- **No Splunk Cloud validation** - tested on Splunk Enterprise only.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

Apache 2.0 - see [LICENSE](package/LICENSE.txt)
