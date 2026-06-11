# Contributing

Thanks for your interest in contributing to the Cloudflare R2 Log Ingestion
Add-on for Splunk.

## Getting started

1. Fork the repository
2. Follow the local development setup in [DEVELOPMENT.md](DEVELOPMENT.md)
3. Make your changes on a feature branch
4. Run AppInspect to verify no regressions: `splunk-appinspect inspect TA-cloudflare-r2-*.tgz --mode precert`
5. Open a pull request with a clear description of the change and why

## What we're looking for

The highest-value contributions right now (see [DEVELOPMENT.md](DEVELOPMENT.md)
for technical details):

- **Credential encryption** via Splunk's `storage/passwords` API
- **Splunk Cloud compatibility** testing and fixes
- **Test coverage** - unit tests for checkpointing logic and R2 client configuration

## Code style

- Python 3, no f-strings (for Splunk 8.x / Python 3.7 compatibility)
- No new external dependencies without a strong reason - keep the vendored
  library footprint small
- Follow the existing pattern: transport only, no field extraction or
  dataset-specific logic in the modular input itself

## Reporting bugs

Open a GitHub issue with:
- Splunk version
- Python version (check `$SPLUNK_HOME/bin/python3 --version`)
- The relevant lines from `$SPLUNK_HOME/var/log/splunk/splunkd.log`
- Steps to reproduce
