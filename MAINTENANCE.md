# Maintenance and Troubleshooting Guide
# Cloudflare R2 Log Ingestion Add-on for Splunk (TA-cloudflare-r2)

This document is the single source of truth for anyone maintaining, troubleshooting,
or taking ownership of this add-on. It is written to be self-contained - a colleague
with only this file and the source code should be able to carry the project forward.

---

## Project context

### What this is and why it exists

The Splunk Add-on for AWS (Splunkbase 1876) fails with Cloudflare R2 because it
calls `sts.get_caller_identity()` (AWS STS) to validate credentials. R2 is
S3-compatible but has no STS service. This is a confirmed dead-end as of v8.1.2 -
no configuration workaround exists, including the Generic S3 input with custom
`host_name`. See DEVELOPMENT.md for full technical detail.

This add-on solves the gap by using boto3 directly with SigV4 auth and path-style
S3 calls against the R2 endpoint - no STS dependency.

### Repository

- **GitHub**: https://github.com/kowalified/splunk-ta-cloudflare-r2
  *(pending transfer to Cloudflare's official GitHub org - check current location)*
- **FOSS approval ticket**: FLOSS-431 (Cloudflare internal Jira)

### How the add-on works (30-second version)

```
Every <interval> seconds:
  1. ListObjectsV2(Bucket, Prefix, StartAfter=last_key)  ← only keys after checkpoint
  2. For each new .log.gz file:
     a. GetObject
     b. gzip.decompress
     c. Split on newlines
     d. Emit each line as one Splunk event
     e. Save checkpoint (last_key = this file's key)
```

Checkpoint file location:
`$SPLUNK_HOME/var/lib/splunk/modinputs/cloudflare_r2/<stanza_name>.json`

---

## Local development environment

### Prerequisites

- Docker (OrbStack recommended on Apple Silicon Mac)
- Python 3.10+ for running seed_test_data.py
- `pip install boto3` for seed script
- `pip install splunk-appinspect` for package validation

### Start Splunk locally

**Important**: `splunk/splunk` is `linux/amd64` only. On Apple Silicon it runs
under Rosetta 2 emulation - first boot takes 3-5 minutes. This is normal.

```bash
docker run -d \
  --name splunk-dev \
  -p 8000:8000 -p 8089:8089 \
  -e SPLUNK_START_ARGS=--accept-license \
  -e SPLUNK_GENERAL_TERMS=--accept-sgt-current-at-splunk-com \
  -e SPLUNK_PASSWORD=changeme1! \
  -e SPLUNK_LICENSE_URI=/tmp/splunk.lic \
  -v /path/to/Splunk.License:/tmp/splunk.lic:ro \
  -v $(pwd)/TA-cloudflare-r2:/opt/splunk/etc/apps/TA-cloudflare-r2 \
  splunk/splunk:latest
```

The volume mount means code changes take effect on the next poll - no rebuild needed.

### Verify Splunk is healthy

```bash
curl -sk https://localhost:8089/services/server/info -u admin:changeme1! | grep version
```

### Watch the modular input logs

```bash
docker exec --user splunk splunk-dev grep "cloudflare_r2" \
  /opt/splunk/var/log/splunk/splunkd.log | tail -20
```

---

## Release process

### Build the package

```bash
# From the repo root
find TA-cloudflare-r2 -name "*.pyc" -delete 2>/dev/null || true
find TA-cloudflare-r2 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find TA-cloudflare-r2/metadata -name "local.meta" -delete 2>/dev/null || true
xattr -rc TA-cloudflare-r2/ 2>/dev/null || true

COPYFILE_DISABLE=1 tar -czf TA-cloudflare-r2-<version>.tgz \
  --exclude="TA-cloudflare-r2/metadata/local.meta" \
  --exclude="*/__pycache__" \
  --exclude="*/.DS_Store" \
  --exclude="*/._*" \
  TA-cloudflare-r2/
```

### Run AppInspect

```bash
splunk-appinspect inspect TA-cloudflare-r2-<version>.tgz --mode precert
```

Expected result: `0 errors | 0 failures | 0 future_failures`

Known warnings (all acceptable, do not require fixes):
- subprocess usage in vendored dateutil (not our code)
- Outdated splunk-sdk warning (update splunklib when upgrading)
- admin role not available in Splunk Cloud (update metadata for Cloud deployments)
- inputs.conf not synced to indexers in Victoria (expected for modular inputs)

### Tag and publish

```bash
git tag -a v<version> -m "v<version> - <description>"
git push origin v<version>
```

Then create a GitHub Release at the repo URL, attach the `.tgz` as a release asset.
**Do not commit the `.tgz` to the repo** - it is gitignored and belongs only as a
release asset.

---

## Troubleshooting scenarios

### Scenario 1: Customer installs the plugin and events are not appearing

**Step 1 - Check if the input is running**
```bash
# On the Splunk server (or via docker exec for local dev):
grep "cloudflare_r2" $SPLUNK_HOME/var/log/splunk/splunkd.log | tail -30
```

Look for `starting poll` lines. If absent, the input is not running - check:
- Is the input enabled? Settings > Data Inputs > Cloudflare R2 Log Ingestion
- Is the TA installed and enabled? Settings > Apps

**Step 2 - Check for errors in the poll log**

Common errors and fixes:

| Error | Cause | Fix |
|---|---|---|
| `SSL: CERTIFICATE_VERIFY_FAILED` | Outbound TLS inspection on the Splunk server's network | Set `verify_ssl = false` in the input, or add the inspection CA cert to the Splunk server's trust store |
| `InvalidAccessKeyId` or `SignatureDoesNotMatch` | Wrong R2 credentials | Regenerate the R2 API token in Cloudflare dashboard and update the input |
| `NoSuchBucket` | Bucket name typo or wrong account_id | Verify both in Cloudflare dashboard |
| `poll complete: files=0 events=0` repeatedly | Checkpoint is ahead of all files in the bucket | Reset checkpoint (see below), or check key_prefix matches where Logpush is writing |
| `account_id must be a 32-character hex string` | Wrong account_id format | account_id is the 32-char hex from the dashboard URL, not the API token |

**Step 3 - Verify R2 connectivity from the Splunk server**

```python
# Run on the Splunk server (or inside the container)
python3 -c "
import boto3
from botocore.config import Config
client = boto3.client('s3',
    endpoint_url='https://<ACCOUNT_ID>.r2.cloudflarestorage.com',
    aws_access_key_id='<ACCESS_KEY_ID>',
    aws_secret_access_key='<SECRET_ACCESS_KEY>',
    region_name='auto',
    config=Config(signature_version='s3v4', s3={'addressing_style':'path'}),
)
resp = client.list_objects_v2(Bucket='<BUCKET_NAME>', MaxKeys=5)
print('Connected. Objects:', resp['KeyCount'])
for o in resp.get('Contents', []):
    print(' ', o['Key'])
"
```

**Step 4 - Check the checkpoint**

```bash
# Find checkpoint files
ls $SPLUNK_HOME/var/lib/splunk/modinputs/cloudflare_r2/

# Read a checkpoint
cat $SPLUNK_HOME/var/lib/splunk/modinputs/cloudflare_r2/<stanza_name>.json
```

The `last_key` value should be lexicographically earlier than the newest file in R2.
If the checkpoint is ahead of all R2 files (can happen after testing or if the
bucket was recreated), reset it:

```bash
# Reset all checkpoints for this scheme
splunk clean inputdata cloudflare_r2

# Or delete a single checkpoint file
rm $SPLUNK_HOME/var/lib/splunk/modinputs/cloudflare_r2/<stanza_name>.json
```

**Step 5 - Verify Logpush is actually writing files**

If R2 connectivity is confirmed but there are no files, check the Logpush job:
- Cloudflare dashboard → Account/Zone → Logs → Logpush
- Check "Job Health" - should show green with recent push timestamps
- If job shows errors, check the R2 bucket permissions on the Logpush-managed token

---

### Scenario 2: GitHub issue opened by a community user

**First response checklist:**

1. Ask for the relevant lines from `splunkd.log` (see Scenario 1 Step 1)
2. Ask for Splunk version: `splunk version`
3. Ask for Python version: `$SPLUNK_HOME/bin/python3 --version`
4. Ask what operating system / deployment type (Enterprise on-prem, Splunk Cloud, etc.)
5. Confirm they're using the `verify_ssl` setting correctly for their network

**Common community issues:**

- **"I installed it but I don't see any events"** → Usually a disabled input or SSL error. Walk through Scenario 1.
- **"I get duplicate events after restarting Splunk"** → Checkpoint not persisting. Check the checkpoint path (see `checkpoint_dir` in splunkd.log). May indicate a permissions issue on `modinputs/cloudflare_r2/`.
- **"The input shows as enabled but never fires"** → Check interval setting. Also check if `python.version = python3` is set in inputs.conf - Splunk 9.x requires this.
- **"I get an error about account_id"** → The account_id is the 32-char hex in the Cloudflare dashboard URL (`dash.cloudflare.com/<ACCOUNT_ID>/...`), not the API token value.

---

### Scenario 3: Testing against a new Splunk release

**Test matrix checklist:**

1. Pull the new Splunk Docker image: `docker pull --platform linux/amd64 splunk/splunk:<new_version>`
2. Start a fresh container (no volume mount - test the installed package)
3. Install the `.tgz` via Splunk UI (Apps > Manage Apps > Install app from file)
4. Create a test input pointing at a real or seeded R2 bucket
5. Verify events appear: `index=<test_index> | stats count by sourcetype`
6. Restart Splunk, verify zero duplicate events (checkpoint test)
7. Run AppInspect against the package with the new Splunk version

**What commonly breaks between Splunk versions:**

- Python version bump: Splunk ships a newer Python - check boto3 compatibility
  (`boto3` dropped Python 3.9 support in 2026; if Splunk ships 3.13+, update boto3)
- `python.required` value: AppInspect may require updating the version string
  in `default/inputs.conf`
- splunklib version: Update `bin/lib/splunklib/` if AppInspect warns about outdated SDK
- New AppInspect checks: A previously passing package may gain new failures

**Updating vendored dependencies:**

```bash
# Re-vendor boto3 (from inside a Linux container to get the right platform)
# OR use pure-Python wheels (all current deps are py3-none-any)
pip install boto3==<version> \
  --target TA-cloudflare-r2/bin/lib \
  --no-deps --upgrade

# Update all deps
pip install boto3 botocore s3transfer jmespath python-dateutil urllib3 six \
  --target TA-cloudflare-r2/bin/lib --upgrade

# Re-run AppInspect
splunk-appinspect inspect TA-cloudflare-r2-<version>.tgz --mode precert
```

---

### Scenario 4: Something on the Cloudflare side has changed

**R2 S3 API changes:**

The Cloudflare R2 S3 API is documented at:
https://developers.cloudflare.com/r2/api/s3/api/

The add-on uses only: `ListObjectsV2`, `GetObject`. If either of these changes
behavior, it will appear as either errors in `splunkd.log` or missing/incorrect events.

Key R2 configuration assumptions that must remain true:
- Endpoint format: `https://<account_id>.r2.cloudflarestorage.com`
- Path-style addressing required (`addressing_style: path`)
- Region: `auto` (or `us-east-1` as alias)
- SigV4 authentication with static access keys
- No STS dependency anywhere in the auth flow

**Logpush key naming changes:**

The checkpoint relies on lexicographic ordering of R2 keys. The current Logpush
key format is:
```
<prefix>/<YYYYMMDD>/<YYYYMMDDTHHmmSSZ>_<YYYYMMDDTHHmmSSZ>_<random>.log.gz
```

If Cloudflare changes this format such that new keys sort *before* older keys,
the checkpoint logic breaks and events will be missed. Validate the key format
by checking real files in a Logpush bucket when investigating missing event issues.

**Logpush field schema changes:**

The add-on is dataset-agnostic - it passes raw JSON lines to Splunk without
parsing. Field schema changes in Cloudflare Logpush datasets do not affect the
add-on's core transport behavior. However, if the Cloudflare App for Splunk
(4501) dashboards reference specific fields, those dashboards may need updating
separately (not in scope for this add-on).

To get the current schema for any dataset:
```bash
curl -s "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/logpush/datasets/<DATASET>/fields" \
  -H "Authorization: Bearer <API_TOKEN>" | python3 -m json.tool
```

---

### Scenario 5: New maintainer taking ownership

Welcome. This section tells you everything you need to know to carry this forward.

**What this project is:**

A Splunk Technology Add-on (TA) that ingests Cloudflare Logpush files from
Cloudflare R2 into Splunk. It exists because the official Splunk Add-on for AWS
cannot work with R2 (STS dependency, no workaround). Full background is in
DEVELOPMENT.md.

**Repository structure:**

```
splunk-ta-cloudflare-r2/
├── README.md                          # User-facing installation and config guide
├── DEVELOPMENT.md                     # Technical design and contributor guide
├── CONTRIBUTING.md                    # How to contribute
├── SECURITY.md                        # Security policy and known considerations
├── MAINTENANCE.md                     # This file
├── seed_test_data.py                  # Dev utility: upload synthetic test data to R2
└── TA-cloudflare-r2/
    ├── bin/
    │   ├── cloudflare_r2.py           # THE modular input - all core logic is here
    │   └── lib/                       # Vendored Python deps (boto3, splunklib, etc.)
    ├── default/
    │   ├── app.conf                   # App metadata
    │   ├── inputs.conf                # Default stanza (disabled, interval=300)
    │   └── data/ui/manager/
    │       └── cloudflare_r2.xml      # Splunk UI form for creating inputs
    ├── README/
    │   └── inputs.conf.spec           # Parameter definitions (required by Splunk)
    ├── metadata/
    │   └── default.meta               # Access control
    └── LICENSE                        # Apache 2.0
```

**The one file that matters most:** `TA-cloudflare-r2/bin/cloudflare_r2.py`

It is ~350 lines of pure Python with no exotic patterns. Read it top to bottom
and you'll understand everything the add-on does. The key functions are:
- `_make_r2_client()` - builds the boto3 client with R2 config
- `_list_new_objects()` - ListObjectsV2 with StartAfter pagination
- `_process_object()` - downloads, decompresses, emits events
- `_load_checkpoint()` / `_save_checkpoint()` - JSON file in modinputs dir
- `CloudflareR2Input.stream_events()` - Splunk calls this on each poll interval

**Known gaps that the next version should address (priority order):**

1. Credential encryption - secret_access_key is in plaintext inputs.conf.
   Fix: custom REST handler + storage/passwords. See DEVELOPMENT.md.
2. Splunk Cloud compatibility - untested on Victoria experience.
3. `verify_ssl` - boolean flag is a blunt instrument; a `ca_bundle` path
   parameter would be cleaner for TLS inspection environments.

**Testing checklist for any change:**

1. Start local Splunk (see above)
2. Create a test input or use `seed_test_data.py` to populate an R2 bucket
3. Verify events appear: `index=<test_index> | stats count by sourcetype`
4. Restart Splunk, verify zero duplicate events
5. Run AppInspect: `splunk-appinspect inspect TA-cloudflare-r2-*.tgz --mode precert`
6. Expected: `0 errors | 0 failures | 0 future_failures`

---

### Scenario 6: Updating vendored dependencies after boto3 breaking change

boto3 is the most likely dependency to cause issues over time (it dropped Python
3.9 support in 2026 and may introduce breaking changes in future major versions).

**Symptoms of a boto3 version issue:**
- `ImportError` in splunkd.log when the input starts
- `AttributeError` or `TypeError` from within botocore
- AppInspect warning about outdated SDK

**Update process:**

```bash
# 1. Check what version is currently vendored
cat TA-cloudflare-r2/bin/lib/botocore/__init__.py | grep __version__

# 2. Clear old versions
rm -rf TA-cloudflare-r2/bin/lib/boto3* \
       TA-cloudflare-r2/bin/lib/botocore* \
       TA-cloudflare-r2/bin/lib/s3transfer* \
       TA-cloudflare-r2/bin/lib/jmespath* \
       TA-cloudflare-r2/bin/lib/dateutil* \
       TA-cloudflare-r2/bin/lib/urllib3* \
       TA-cloudflare-r2/bin/lib/six*

# 3. Reinstall - all packages are pure Python (py3-none-any), platform-independent
pip install boto3 botocore s3transfer jmespath python-dateutil urllib3 six \
  --target TA-cloudflare-r2/bin/lib --no-deps

# 4. Prune botocore data directory (keep only s3 and sts)
cd TA-cloudflare-r2/bin/lib/botocore/data
for item in */; do
  case "${item%/}" in
    s3|sts) ;;
    *) rm -rf "$item" ;;
  esac
done
cd -

# 5. Update version in app.conf and README
# 6. Build package and run AppInspect
# 7. Test end-to-end
```

---

### Scenario 7: AppInspect adds a new check that fails

AppInspect releases updates periodically. A package that passed before may gain
new failures when AppInspect is updated.

**Process:**
1. Run: `splunk-appinspect inspect TA-cloudflare-r2-*.tgz --mode precert 2>&1 | grep FAILURE`
2. For each failure, read the description carefully
3. If the failure is in **our code** (`bin/cloudflare_r2.py`): fix it
4. If the failure is in **vendored libraries** (boto3/botocore/urllib3): it may
   require either a code change in the vendored library, or a waiver request to
   Splunk explaining it's a false positive in a standard open-source dependency.
   The UDP socket check in botocore was previously resolved by setting
   `_create_csm_monitor()` to return `None` - a similar surgical approach may
   work for new false positives.

---

## Key reference links

| Resource | URL |
|---|---|
| Cloudflare R2 S3 API docs | https://developers.cloudflare.com/r2/api/s3/api/ |
| Cloudflare Logpush docs | https://developers.cloudflare.com/logs/logpush/ |
| Logpush dataset field schemas | https://developers.cloudflare.com/logs/logpush/logpush-job/datasets/ |
| Splunk modular input docs | https://dev.splunk.com/enterprise/docs/developapps/manageknowledge/custominputs/modinputsoverview/ |
| Splunk manager XML reference | https://dev.splunk.com/enterprise/docs/developapps/manageknowledge/custominputs/modinputsexample/ |
| AppInspect docs | https://dev.splunk.com/enterprise/docs/releaseapps/appinspect/ |
| Cloudflare App for Splunk (4501) | https://splunkbase.splunk.com/app/4501/ |
| Splunk Add-on for AWS (1876) | https://splunkbase.splunk.com/app/1876/ |
| splunk/splunk Docker Hub | https://hub.docker.com/r/splunk/splunk |
