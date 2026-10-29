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

The R2 secret access key is never written to `inputs.conf`. At configuration
time the add-on stores it in Splunk's encrypted credential store
(`storage/passwords`) via solnlib's `conf_manager`, and the modular input reads
it back from there at run time. No plaintext credential is written to the
add-on's conf files or the app-local filesystem.

The optional proxy password (Configuration > Settings > Proxy) is stored the
same way - encrypted via the credential store, never written in plaintext to
`ta_cloudflare_r2_settings.conf`.

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

The `allow_nonstrict_tls` parameter defaults to `false` (strict). On Python
3.13+ (the bundled interpreter on Splunk 10.4+), `ssl.create_default_context()`
enables OpenSSL's strict X.509 profile checks (`VERIFY_X509_STRICT`) by
default. Setting `allow_nonstrict_tls` to `true` clears only that flag —
`verify_mode` stays `CERT_REQUIRED` and `check_hostname` stays enabled, so the
certificate chain (against the configured CA bundle) and hostname are still
fully verified. This is a narrower, lower-risk relaxation than `verify_ssl`,
scoped to RFC 5280 profile strictness rather than verification as a whole;
enable it only to work around a TLS-inspecting proxy whose re-signed
certificates fail that specific check (e.g. a missing Authority Key
Identifier extension).
