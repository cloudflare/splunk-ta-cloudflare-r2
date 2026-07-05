# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Integration-style tests for cloudflare_r2_helper.stream_events(), the actual
poll loop: dedupe, at-least-once/no-checkpoint-on-corrupt-object, non-Logpush
suffix skip, ts-based prune, and the KV-unreachable/untyped abort-without-
emit guards.

R2 access (R2Client), account resolution (_get_account), and the KV Store
collection (_kv_collection) are patched with test doubles; the KV Store
itself is tests/_fake_kvstore.py, the same fake used by
test_helper_checkpoint.py, so dedupe/prune here exercise the real
_load_processed()/_assert_typed() code paths, not a re-mocked version of
them.

`now = datetime.datetime.now(...)` inside stream_events() is real wall-clock
time and not injectable (unlike _window_floor's `now` parameter), so
timestamp assertions here use robust real-clock-relative bounds (e.g. "this
checkpoint's ts is within the last few seconds", "this seeded stale row's ts
is decades in the past") rather than exact values.

No real Splunk, no network. See tests/_splunk_stubs.py for the module-load
stubs and why the extra log.*/conf_manager.get_log_level stubs were needed
here specifically (not all call sites in stream_events() are wrapped in a
try/except, unlike the pure-function tests).

Run:  python3 tests/test_helper_stream_events.py        (from the repo root)
  or  python3 -m unittest discover -s tests -v
"""

import datetime
import os
import sys
import time
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))

from _splunk_stubs import install_stubs  # noqa: E402

install_stubs()

import cloudflare_r2_helper as helper  # noqa: E402
from _fake_kvstore import FakeKVCollection  # noqa: E402


FAKE_ACCOUNT = {
    "account_id": "abc123",
    "access_key_id": "AKIDFAKE",
    "secret_access_key": "secretfake",
    "verify_ssl": "true",
    "ca_bundle_path": "",
}


class FakeR2Client:
    """Test double for r2client.R2Client. Configure `.keys` and
    `.lines_by_key` before the call; `.raise_after` maps a key to an
    exception to raise immediately after yielding that key's configured
    lines (simulating a corrupt/truncated object mid-stream)."""

    def __init__(self):
        self.keys = []
        self.lines_by_key = {}
        self.raise_after = {}
        self.iter_object_keys_calls = []
        self.iter_object_lines_calls = []

    def iter_object_keys(self, bucket, prefix=None, start_after=None):
        self.iter_object_keys_calls.append(
            {"bucket": bucket, "prefix": prefix, "start_after": start_after}
        )
        for k in self.keys:
            yield k

    def iter_object_lines(self, bucket, key):
        self.iter_object_lines_calls.append((bucket, key))
        for line in self.lines_by_key.get(key, []):
            yield line
        if key in self.raise_after:
            raise self.raise_after[key]


class FakeInputs:
    """Test double for smi.InputDefinition: only .inputs and
    .metadata["session_key"] are used by stream_events()."""

    def __init__(self, inputs_dict, session_key="fake-session-key"):
        self.inputs = inputs_dict
        self.metadata = {"session_key": session_key}


class FakeEventWriter:
    """Test double for smi.EventWriter: records every event passed to
    write_event() (each is a _splunk_stubs._StubEvent exposing the
    constructor kwargs as attributes: .data/.index/.sourcetype/.source/
    .host)."""

    def __init__(self):
        self.events = []

    def write_event(self, event):
        self.events.append(event)


def _key_within_lookback_window(suffix, minutes_ago=1):
    """Build a Logpush-style key whose date/time is safely inside
    stream_events()'s lookback window as of the REAL current time.

    stream_events() computes its window floor from real wall-clock
    datetime.datetime.now() (unlike _window_floor's own tests, which inject
    `now` directly) - a hardcoded fixed date string would only happen to
    satisfy the "_key >= lo" dedupe comparison on whatever date this test
    suite happened to be written on, and would silently stop being "already
    processed" (breaking dedupe tests in a confusing way) on any other day.
    Compute it live instead so these tests are correct regardless of when
    they actually run.
    """
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=minutes_ago
    )
    return "{}/{}_{}".format(dt.strftime("%Y%m%d"), dt.strftime("%Y%m%dT%H%M%SZ"), suffix)


def _input_stanza(**overrides):
    stanza = {
        "account": "my_account",
        "bucket_name": "my-bucket",
        "key_prefix": "",
        "sourcetype": "cloudflare:json",
        "index": "main",
        "lookback_days": "1",
    }
    stanza.update(overrides)
    return stanza


class StreamEventsTestCase(unittest.TestCase):
    """Common patching for _get_account and _kv_collection, applied per
    test via setUp so each test only needs to configure the FakeR2Client and
    FakeKVCollection it cares about."""

    def setUp(self):
        self.collection = FakeKVCollection(field_ts_type="number")
        self.fake_client = FakeR2Client()

        self._get_account_patch = mock.patch.object(
            helper, "_get_account", return_value=dict(FAKE_ACCOUNT)
        )
        self._kv_collection_patch = mock.patch.object(
            helper, "_kv_collection", return_value=self.collection
        )
        self._r2client_patch = mock.patch.object(
            helper, "R2Client", return_value=self.fake_client
        )
        self._get_account_patch.start()
        self._kv_collection_patch.start()
        self._r2client_patch.start()
        self.addCleanup(self._get_account_patch.stop)
        self.addCleanup(self._kv_collection_patch.stop)
        self.addCleanup(self._r2client_patch.stop)

    def run_stream_events(self, **stanza_overrides):
        inputs = FakeInputs({"cloudflare_r2://my_input": _input_stanza(**stanza_overrides)})
        writer = FakeEventWriter()
        helper.stream_events(inputs, writer)
        return writer


class TestDedupe(StreamEventsTestCase):

    def test_already_processed_key_is_skipped(self):
        key = _key_within_lookback_window("abc.log.gz")
        self.fake_client.keys = [key]
        self.fake_client.lines_by_key = {key: ["line1", "line2"]}
        # Pre-seed the checkpoint as if this key was already ingested.
        self.collection.data.seed(
            {"_key": "my_input|" + key, "input": "my_input", "ts": int(time.time())}
        )

        writer = self.run_stream_events()

        self.assertEqual(writer.events, [])
        self.assertEqual(len(self.fake_client.iter_object_lines_calls), 0)


class TestCorruptObjectHandling(StreamEventsTestCase):

    def test_corrupt_object_not_checkpointed_and_poll_continues(self):
        good_key = "20260701/20260701T000000Z_good.log.gz"
        bad_key = "20260701/20260701T010000Z_bad.log.gz"
        self.fake_client.keys = [bad_key, good_key]
        self.fake_client.lines_by_key = {
            bad_key: ["partial-line-before-corruption"],
            good_key: ["good-line-1", "good-line-2"],
        }
        self.fake_client.raise_after = {bad_key: zlib.error("bad CRC")}

        writer = self.run_stream_events()

        # The partial line from the corrupt object WAS already written
        # before the exception (at-least-once - a real crash mid-stream
        # would look the same), but the object is not checkpointed, and the
        # later, good object is still fully processed - a single corrupt
        # object does not abort the rest of the poll.
        emitted_data = [e.data for e in writer.events]
        self.assertEqual(
            emitted_data,
            ["partial-line-before-corruption", "good-line-1", "good-line-2"],
        )
        rows = self.collection.data.all_rows()
        checkpointed_keys = {r["_key"] for r in rows}
        self.assertNotIn("my_input|" + bad_key, checkpointed_keys)
        self.assertIn("my_input|" + good_key, checkpointed_keys)

    def test_oserror_mid_stream_also_skipped_not_checkpointed(self):
        # Same guard, different exception type in the documented catch list
        # (transient network/read error, not just corrupt gzip).
        key = "20260701/20260701T000000Z_transient.log.gz"
        self.fake_client.keys = [key]
        self.fake_client.lines_by_key = {key: []}
        self.fake_client.raise_after = {key: OSError("connection reset")}

        writer = self.run_stream_events()

        self.assertEqual(writer.events, [])
        self.assertEqual(self.collection.data.all_rows(), [])

    def test_unrelated_exception_type_propagates_uncaught_by_the_guard(self):
        # Only R2Error/OSError/EOFError/zlib.error are caught by the
        # corrupt-object guard - confirm something outside that list is NOT
        # silently swallowed the same way (it's caught by stream_events()'s
        # outer per-input handler instead, which logs and returns rather
        # than propagating out of stream_events() itself - but the object
        # must still not be checkpointed, and no later keys in this same
        # input get a chance to run since the whole input's poll stops).
        raising_key = "20260701/20260701T000000Z_weird.log.gz"
        later_key = "20260701/20260701T020000Z_later.log.gz"
        self.fake_client.keys = [raising_key, later_key]
        self.fake_client.lines_by_key = {raising_key: [], later_key: ["line"]}
        self.fake_client.raise_after = {raising_key: ValueError("not a caught type")}

        writer = self.run_stream_events()

        self.assertEqual(writer.events, [])
        self.assertEqual(self.collection.data.all_rows(), [])
        # later_key was never reached because the ValueError propagated out
        # of the for-loop entirely, straight to the outer per-input handler.
        self.assertNotIn(later_key, [k for _, k in self.fake_client.iter_object_lines_calls])


class TestNonLogpushSuffix(StreamEventsTestCase):

    def test_key_not_ending_in_log_gz_is_skipped_before_fetching(self):
        marker_key = "20260701/20260701T000000Z_marker.json"
        real_key = "20260701/20260701T010000Z_real.log.gz"
        self.fake_client.keys = [marker_key, real_key]
        self.fake_client.lines_by_key = {real_key: ["a-line"]}

        writer = self.run_stream_events()

        # The non-Logpush key must never even reach iter_object_lines.
        fetched_keys = [k for _, k in self.fake_client.iter_object_lines_calls]
        self.assertEqual(fetched_keys, [real_key])
        self.assertEqual([e.data for e in writer.events], ["a-line"])


class TestSuccessfulIngest(StreamEventsTestCase):

    def test_full_file_checkpointed_only_after_all_lines_emitted(self):
        key = "20260701/20260701T000000Z_ok.log.gz"
        self.fake_client.keys = [key]
        self.fake_client.lines_by_key = {key: ["l1", "l2", "l3"]}

        writer = self.run_stream_events()

        self.assertEqual([e.data for e in writer.events], ["l1", "l2", "l3"])
        rows = self.collection.data.all_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["_key"], "my_input|" + key)
        self.assertEqual(row["input"], "my_input")
        # ts is real wall-clock time (not injectable) - assert it's an int
        # recorded within the last few seconds, not an exact value.
        self.assertIsInstance(row["ts"], int)
        self.assertLess(abs(row["ts"] - time.time()), 5)

    def test_event_fields_are_populated_correctly(self):
        key = "gateway_dns/20260701/20260701T000000Z_x.log.gz"
        self.fake_client.keys = [key]
        self.fake_client.lines_by_key = {key: ["{\"a\": 1}"]}

        writer = self.run_stream_events(
            bucket_name="my-bucket", sourcetype="cloudflare:dns", index="cf_logs"
        )

        self.assertEqual(len(writer.events), 1)
        event = writer.events[0]
        self.assertEqual(event.data, "{\"a\": 1}")
        self.assertEqual(event.sourcetype, "cloudflare:dns")
        self.assertEqual(event.index, "cf_logs")
        self.assertEqual(event.source, "r2://my-bucket/" + key)
        self.assertEqual(event.host, "cloudflare-r2")


class TestPrune(StreamEventsTestCase):

    def test_prune_removes_only_stale_rows_for_this_input(self):
        ancient = int(time.time()) - 10 * 365 * 86400  # ~10 years ago
        recent = int(time.time())

        self.collection.data.seed(
            {"_key": "my_input|stale", "input": "my_input", "ts": ancient},
            {"_key": "my_input|fresh", "input": "my_input", "ts": recent},
            {"_key": "other_input|stale", "input": "other_input", "ts": ancient},
        )
        self.fake_client.keys = []  # nothing new to ingest this poll

        self.run_stream_events(lookback_days="1")

        remaining_keys = {r["_key"] for r in self.collection.data.all_rows()}
        self.assertEqual(
            remaining_keys, {"my_input|fresh", "other_input|stale"}
        )


class TestKVStoreGuards(StreamEventsTestCase):

    def test_kv_unreachable_aborts_without_emitting(self):
        self._kv_collection_patch.stop()
        with mock.patch.object(
            helper, "_kv_collection", side_effect=RuntimeError("KV Store down")
        ):
            self.fake_client.keys = ["20260701/20260701T000000Z_x.log.gz"]
            writer = self.run_stream_events()
        self.assertEqual(writer.events, [])
        # Never even got far enough to list R2 objects.
        self.assertEqual(self.fake_client.iter_object_keys_calls, [])
        self._kv_collection_patch.start()  # restore for addCleanup's stop()

    def test_untyped_collection_aborts_without_emitting(self):
        untyped_collection = FakeKVCollection(field_ts_type="string")
        self._kv_collection_patch.stop()
        with mock.patch.object(
            helper, "_kv_collection", return_value=untyped_collection
        ):
            self.fake_client.keys = ["20260701/20260701T000000Z_x.log.gz"]
            writer = self.run_stream_events()
        self.assertEqual(writer.events, [])
        self.assertEqual(self.fake_client.iter_object_keys_calls, [])
        self._kv_collection_patch.start()


class TestAccountWiring(StreamEventsTestCase):

    def test_r2client_constructed_from_account_fields(self):
        self.fake_client.keys = []
        captured = {}

        def _fake_r2client_ctor(**kwargs):
            captured.update(kwargs)
            return self.fake_client

        with mock.patch.object(helper, "R2Client", side_effect=_fake_r2client_ctor):
            self.run_stream_events()

        self.assertEqual(captured["account_id"], FAKE_ACCOUNT["account_id"])
        self.assertEqual(captured["access_key"], FAKE_ACCOUNT["access_key_id"])
        self.assertEqual(captured["secret_key"], FAKE_ACCOUNT["secret_access_key"])
        self.assertTrue(captured["verify_ssl"])
        self.assertIsNone(captured["ca_bundle"])

    def test_ca_bundle_path_is_passed_through_when_set(self):
        with mock.patch.object(
            helper, "_get_account",
            return_value=dict(FAKE_ACCOUNT, ca_bundle_path="/etc/certs/ca.pem"),
        ):
            captured = {}

            def _fake_r2client_ctor(**kwargs):
                captured.update(kwargs)
                return self.fake_client

            with mock.patch.object(helper, "R2Client", side_effect=_fake_r2client_ctor):
                self.run_stream_events()

        self.assertEqual(captured["ca_bundle"], "/etc/certs/ca.pem")

    def test_verify_ssl_false_is_wired_through(self):
        with mock.patch.object(
            helper, "_get_account", return_value=dict(FAKE_ACCOUNT, verify_ssl="false"),
        ):
            captured = {}

            def _fake_r2client_ctor(**kwargs):
                captured.update(kwargs)
                return self.fake_client

            with mock.patch.object(helper, "R2Client", side_effect=_fake_r2client_ctor):
                self.run_stream_events()

        self.assertFalse(captured["verify_ssl"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
