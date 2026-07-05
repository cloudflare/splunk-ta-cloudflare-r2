# Copyright (c) 2026 Cloudflare, Inc.
# Licensed under the Apache 2.0 license.

"""
Shared test helper: stub out UCC/Splunk-supplied imports so
package/bin/cloudflare_r2_helper.py can be imported in isolation.

cloudflare_r2_helper.py imports three things that don't exist outside a real
Splunk install or after `ucc-gen build` has run:

  - import_declare_test  (a UCC-generated sys.path shim, only present in the
    built output/ tree, never in package/bin/ source)
  - solnlib              (Splunk-supplied: conf_manager, log,
    splunk_rest_client.SplunkRestClient)
  - splunklib            (Splunk-supplied: modularinput)

None of that is needed to test pure logic (window-floor math, prefix
normalization, checkpoint dedupe against a fake KV Store, etc.) -- it's only
needed to satisfy module-level `import` statements so the module can load at
all. install_stubs() registers minimal fake modules in sys.modules before any
test imports cloudflare_r2_helper, and is safe to call multiple times (e.g.
once per test file) or alongside a real Splunk environment (it only stubs a
name if nothing already provides it).

Usage, at the top of a test file, BEFORE importing cloudflare_r2_helper:

    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "package", "bin"))
    from _splunk_stubs import install_stubs
    install_stubs()
    from cloudflare_r2_helper import _window_floor  # noqa: E402
"""

import sys
import types


def _ensure_module(name):
    """Register a bare ModuleType at sys.modules[name] if not already present."""
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


def install_stubs():
    """Install minimal stand-ins for import_declare_test/solnlib/splunklib.

    Idempotent and non-destructive: if a real install already provides one of
    these (e.g. a future CI job that does have solnlib installed), this
    leaves it untouched rather than overwriting it.
    """
    _ensure_module("import_declare_test")

    solnlib = _ensure_module("solnlib")
    solnlib.conf_manager = _ensure_module("solnlib.conf_manager")
    solnlib.log = _ensure_module("solnlib.log")
    solnlib.splunk_rest_client = _ensure_module("solnlib.splunk_rest_client")
    if not hasattr(solnlib.splunk_rest_client, "SplunkRestClient"):
        solnlib.splunk_rest_client.SplunkRestClient = object

    splunklib = _ensure_module("splunklib")
    smi = _ensure_module("splunklib.modularinput")
    splunklib.modularinput = smi
    # cloudflare_r2_helper.py references these as function-signature type
    # annotations (smi.ValidationDefinition, smi.InputDefinition,
    # smi.EventWriter). Annotations are evaluated eagerly at module-load time
    # on Python 3.9-3.13 (this project's supported range), so these names
    # must resolve to *something* even though the annotations are never
    # actually type-checked at runtime - `object` is enough. (Python 3.14+
    # deferred/lazy annotation evaluation - PEP 649 - would silently mask a
    # missing stub here, which is exactly what happened during development:
    # this passed on a local 3.14 interpreter and only failed on the real
    # 3.9/3.13 CI matrix. Don't trust a bare local run on this file as
    # sufficient verification if your local Python is newer than 3.13.)
    if not hasattr(smi, "ValidationDefinition"):
        smi.ValidationDefinition = object
    if not hasattr(smi, "InputDefinition"):
        smi.InputDefinition = object
    if not hasattr(smi, "EventWriter"):
        smi.EventWriter = object
    if not hasattr(smi, "Event"):
        class _StubEvent:
            """Stand-in for splunklib.modularinput.Event: stores constructor
            kwargs as attributes so a test can assert what stream_events()
            actually built (data, index, sourcetype, source, host, ...)."""

            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        smi.Event = _StubEvent
