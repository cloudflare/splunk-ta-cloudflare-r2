# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Cloudflare R2 Log Ingestion - UCC input helper.

Contract (UCC globalConfig + inputHelperModule path, ucc-gen 5.52.0):
the generated input script imports the TOP-LEVEL functions `validate_input`
and `stream_events` from this module (NOT self-methods).

Design (see internal UCC_EXECUTION_PLAN.md 4.6 / 4.6.1):
  - Credentials come from the UCC "account" (secret_access_key is encrypted in
    storage/passwords); read here via solnlib.conf_manager.
  - R2 access is the pure standard-library client in r2client.py (no boto3).
  - Dedupe/checkpoint uses a TYPED KV Store processed-key collection, scoped per
    input, NOT a single monotonic StartAfter cursor -- so a late-delivered
    Logpush batch whose key sorts before earlier ones is still ingested.

  Per poll, for each input:
    floor = now - lookback_days, rendered second-precision as the Logpush key
            lower bound "{prefix}{YYYYMMDD}/{YYYYMMDDTHHMMSSZ}".
    lo    = "{input}|{floor}"  (dedupe lower bound on _key).
    1. S10 type-guard: refuse to run unless collection field.ts == number.
    2. KV-unreachable guard: if the collection can't be read, abort the input
       WITHOUT emitting (never ingest without the dedupe set -> no dup storm).
    3. Load already-processed _keys for THIS input in the window via a PAGED
       range query {$and:[{input},{_key:{$gte:lo}}]} (paging avoids the ~50k
       maxrows truncation that a single query would hit on a busy bucket).
    4. List objects with start_after=floor; skip non-".log.gz"; skip processed;
       emit each object's NDJSON lines; checkpoint per FULL file (at-least-once:
       a mid-file failure is NOT checkpointed and is retried next poll).
    5. Prune this input's rows with ts < now-(lookback*86400+grace) so the
       collection stays bounded. Prune is ts-based (NOT _key-based): in the
       no-date-prefix full-scan fallback there is no _key window, so only a ts
       cutoff can bound the set. field.ts is typed + accelerated for exactly
       this.

  KV Store client-API notes verified on Splunk 9.4 (do not "fix" without
  re-testing there):
    - query(query={dict}, ...) auto-JSON-encodes the dict value.
    - delete(query=<STRING>) does NOT encode -> pass json.dumps(...).
    - batch_save(dict, ...) upserts by _key (idempotent).
    - a typed field surfaces as collection.content["field.ts"] == "number".
"""

import datetime
import json

import import_declare_test  # noqa: F401  (UCC-generated sys.path setup for lib/)

from solnlib import conf_manager, log
from solnlib.splunk_rest_client import SplunkRestClient
from splunklib import modularinput as smi

from r2client import R2Client, R2Error

ADDON_NAME = "TA_cloudflare_r2"
_CONF_LOWER = ADDON_NAME.lower()  # "ta_cloudflare_r2"
_ACCOUNT_CONF = _CONF_LOWER + "_account"
_SETTINGS_CONF = _CONF_LOWER + "_settings"
_CHECKPOINT_COLLECTION = ADDON_NAME + "_processed_keys"

# Cloudflare Logpush writes gzipped NDJSON objects with this suffix. Objects not
# ending in it (e.g. debug/marker files) are skipped and never checkpointed.
# Invariant, so hardcoded; promote-to-config candidate noted in MAINTENANCE.md.
_LOGPUSH_SUFFIX = ".log.gz"

# KV Store query page size. Splunk caps a single query at ~50k rows; we page
# under that so a busy bucket's window set is never silently truncated.
_PAGE = 10000

# Keep a processed key for the full lookback window plus one hour of slack
# (list eventual-consistency / clock skew) before it becomes eligible for prune.
_PRUNE_GRACE_SECONDS = 3600


def _logger(input_name):
    return log.Logs().get_logger("{}_{}".format(_CONF_LOWER, input_name))


def _get_account(session_key, account_name):
    """Return the account stanza as a dict (secret_access_key decrypted)."""
    cfm = conf_manager.ConfManager(
        session_key,
        ADDON_NAME,
        realm="__REST_CREDENTIAL__#{}#configs/conf-{}".format(ADDON_NAME, _ACCOUNT_CONF),
    )
    return cfm.get_conf(_ACCOUNT_CONF).get(account_name)


def _as_bool(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "")


def _normalize_prefix(key_prefix):
    """Empty -> "" (whole bucket). Otherwise ensure a single trailing slash so
    the prefix precedes the date folder cleanly."""
    if not key_prefix:
        return ""
    key_prefix = key_prefix.strip()
    if not key_prefix:
        return ""
    return key_prefix if key_prefix.endswith("/") else key_prefix + "/"


def _window_floor(prefix, lookback_days, now=None):
    """Second-precision lexicographic lower bound on the OBJECT KEY for the
    lookback window.

    Logpush layout (recon'd): "{prefix}{YYYYMMDD}/{YYYYMMDDTHHMMSSZ}_..._{hash}.log.gz"
    where {prefix} is empty or a dataset prefix that PRECEDES the date folder.
    The floor is "{prefix}{floor:%Y%m%d}/{floor:%Y%m%dT%H%M%SZ}", which sorts at
    or below every in-window key and never above one (so start_after never skips
    an in-window key). Correctness is guaranteed by the KV dedupe set; this only
    bounds the ListObjectsV2 scan cost.

    NOTE: if a customer points key_prefix AT a specific date folder (e.g.
    "20260702/"), the injected floor date makes the floor sort below that
    folder's keys -- still correct (no skips), but the scan is not reduced.
    Intended usage is key_prefix = "" or a dataset prefix.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    floor = now - datetime.timedelta(days=lookback_days)
    return "{}{}/{}".format(
        prefix, floor.strftime("%Y%m%d"), floor.strftime("%Y%m%dT%H%M%SZ")
    )


def _kv_collection(session_key):
    """Return the checkpoint collection ENTITY (for .content type-guard and
    .data ops). Raises if KV Store / the collection is unreachable so the caller
    can abort WITHOUT emitting (emitting without the dedupe set would dup)."""
    rc = SplunkRestClient(session_key, ADDON_NAME)
    kv = rc.kvstore
    # NB: rc.kvstore is a KVStoreCollections manager with no .refresh(); the
    # membership check below already issues a live server fetch, so no refresh
    # is needed here (calling it raises AttributeError -> false "untyped" abort).
    if _CHECKPOINT_COLLECTION not in kv:
        raise RuntimeError(
            "KV Store collection '{}' not found -- is collections.conf deployed "
            "and KV Store initialized?".format(_CHECKPOINT_COLLECTION)
        )
    return kv[_CHECKPOINT_COLLECTION]


def _assert_typed(collection):
    """S10 fail-loud guard: refuse to run against an untyped collection, so
    ts-based pruning can never silently no-op on a wrong-typed field."""
    collection.refresh()
    ts_type = collection.content.get("field.ts")
    if ts_type != "number":
        raise RuntimeError(
            "KV Store collection '{}' is not typed (field.ts={!r}, expected "
            "'number'). DROP it and let collections.conf recreate it typed "
            "before running.".format(_CHECKPOINT_COLLECTION, ts_type)
        )


def _load_processed(data, normalized, lo):
    """Return the set of already-processed _keys for THIS input at/above lo,
    paged to avoid the ~50k single-query truncation."""
    processed = set()
    skip = 0
    query = {"$and": [{"input": normalized}, {"_key": {"$gte": lo}}]}
    while True:
        rows = data.query(
            query=query, fields="_key", sort="_key", limit=_PAGE, skip=skip
        )
        if not rows:
            break
        for row in rows:
            processed.add(row["_key"])
        if len(rows) < _PAGE:
            break
        skip += _PAGE
    return processed


def validate_input(definition: smi.ValidationDefinition):
    """Format-only validation. Live R2 checks are deferred to runtime (TLS/CA
    differences at validation time would produce confusing errors)."""
    params = definition.parameters
    bucket = (params.get("bucket_name") or "").strip()
    if not bucket:
        raise ValueError("bucket_name is required")
    lookback = (params.get("lookback_days") or "1").strip()
    if not lookback.isdigit() or int(lookback) < 1:
        raise ValueError("lookback_days must be a positive integer")


def stream_events(inputs: smi.InputDefinition, event_writer: smi.EventWriter):
    for input_name, input_item in inputs.inputs.items():
        normalized = input_name.split("/")[-1]
        logger = _logger(normalized)
        try:
            session_key = inputs.metadata["session_key"]
            try:
                logger.setLevel(
                    conf_manager.get_log_level(
                        logger=logger,
                        session_key=session_key,
                        app_name=ADDON_NAME,
                        conf_name=_SETTINGS_CONF,
                    )
                )
            except Exception:
                pass  # logging tab optional; default level is fine

            log.modular_input_start(logger, normalized)

            # --- KV Store guards BEFORE any listing/emit -----------------------
            # If KV Store is unreachable or untyped we abort this input without
            # emitting, so we never ingest without the dedupe set (S5/S10).
            try:
                collection = _kv_collection(session_key)
                _assert_typed(collection)
                data = collection.data
            except Exception as exc:
                logger.error(
                    "aborting input (no ingest): KV Store checkpoint unavailable "
                    "or untyped: %s", exc,
                )
                log.modular_input_end(logger, normalized)
                continue

            account = _get_account(session_key, input_item.get("account"))
            client = R2Client(
                account_id=account.get("account_id"),
                access_key=account.get("access_key_id"),
                secret_key=account.get("secret_access_key"),
                verify_ssl=_as_bool(account.get("verify_ssl"), default=True),
                ca_bundle=(account.get("ca_bundle_path") or "").strip() or None,
            )

            bucket = input_item.get("bucket_name")
            prefix = _normalize_prefix(input_item.get("key_prefix"))
            sourcetype = input_item.get("sourcetype") or "cloudflare:json"
            index = input_item.get("index")
            lookback_days = int(input_item.get("lookback_days") or "1")

            now = datetime.datetime.now(datetime.timezone.utc)
            floor = _window_floor(prefix, lookback_days, now=now)
            lo = "{}|{}".format(normalized, floor)

            processed = _load_processed(data, normalized, lo)

            total_events = 0
            total_files = 0
            skipped_non_logpush = 0
            for key in client.iter_object_keys(
                bucket, prefix=(prefix or None), start_after=floor
            ):
                if not key.endswith(_LOGPUSH_SUFFIX):
                    skipped_non_logpush += 1
                    logger.debug("skipping non-Logpush object key=%s", key)
                    continue
                ck = "{}|{}".format(normalized, key)
                if ck in processed:
                    continue  # already processed (dedupe)
                try:
                    n = 0
                    for line in client.iter_object_lines(bucket, key):
                        event_writer.write_event(
                            smi.Event(
                                data=line,
                                index=index,
                                sourcetype=sourcetype,
                                source="r2://{}/{}".format(bucket, key),
                                host="cloudflare-r2",
                            )
                        )
                        n += 1
                except (R2Error, OSError) as exc:
                    # Mid-stream failure: do NOT checkpoint so the whole file is
                    # retried next poll (at-least-once; no partial-file state).
                    logger.warning(
                        "skipping key=%s due to error: %s (will retry next poll)",
                        key, exc,
                    )
                    continue
                # Checkpoint the FULL file only after all lines emitted.
                data.batch_save(
                    {"_key": ck, "input": normalized, "ts": int(now.timestamp())}
                )
                total_events += n
                total_files += 1

            # --- Prune this input's stale rows (ts-based, per-input) -----------
            cutoff = int(now.timestamp()) - (lookback_days * 86400 + _PRUNE_GRACE_SECONDS)
            try:
                data.delete(
                    query=json.dumps(
                        {"$and": [{"input": normalized}, {"ts": {"$lt": cutoff}}]}
                    )
                )
            except Exception as exc:
                # Prune failing is non-fatal: dedupe correctness is unaffected;
                # only unbounded growth. Log and continue.
                logger.warning("prune failed (non-fatal): %s", exc)

            if total_events:
                log.events_ingested(
                    logger,
                    input_name,
                    sourcetype,
                    total_events,
                    index,
                    account=input_item.get("account"),
                )
            logger.info(
                "poll complete: files=%s events=%s skipped_non_logpush=%s "
                "bucket=%s prefix=%s floor=%s",
                total_files, total_events, skipped_non_logpush, bucket, prefix, floor,
            )
            log.modular_input_end(logger, normalized)
        except Exception as exc:
            log.log_exception(
                logger, exc, "IngestionError",
                msg_before="Exception while ingesting Cloudflare R2 logs: ",
            )
