# Security Policy

## Reporting a vulnerability

Please do not report security vulnerabilities through public GitHub issues.

Report security vulnerabilities to Cloudflare via:
https://www.cloudflare.com/disclosure/

Include as much detail as possible:
- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Any suggested remediation

## Known security considerations

### Credential storage

In the current version, the R2 secret access key is stored in `inputs.conf`
rather than Splunk's encrypted credential store (`storage/passwords`). This is
a known limitation documented in [DEVELOPMENT.md](DEVELOPMENT.md). The field
renders masked in the Splunk UI but is stored in plaintext on disk.

**Mitigation**: Restrict read access to `$SPLUNK_HOME/etc/apps/TA-cloudflare-r2/`
to the Splunk service account only. Use R2 API tokens scoped to Object Read
on the specific bucket only - not account-level tokens.

### R2 API token scope

When generating R2 API tokens for use with this add-on, scope them to:
- **Permission**: Object Read only (not Read & Write)
- **Bucket**: Specific bucket (not all buckets)

This limits the blast radius if credentials are ever exposed.

### TLS verification

The `verify_ssl` parameter defaults to `true`. Only set it to `false` if your
network performs TLS inspection on outbound traffic and you have confirmed the
inspection CA is from your own organization. Disabling SSL verification exposes
the connection to man-in-the-middle attacks.
