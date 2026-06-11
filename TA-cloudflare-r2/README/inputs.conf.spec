[cloudflare_r2://<name>]
account_id = <value>
* Your Cloudflare account ID (32-character hex string).
* Found in the Cloudflare dashboard URL or R2 overview page.
* Used to construct the R2 endpoint: https://<account_id>.r2.cloudflarestorage.com
* Required.

access_key_id = <value>
* R2 S3-compatible API access key ID.
* Generate via: Cloudflare Dashboard > R2 > Manage R2 API Tokens.
* Required.

secret_access_key = <value>
* R2 S3-compatible API secret access key.
* Shown only once at token creation time. Store securely.
* Splunk encrypts this value in passwords.conf after first use.
* Required.

bucket_name = <value>
* Name of the R2 bucket containing Cloudflare Logpush files.
* Example: my-logpush-bucket
* Required.

verify_ssl = <true|false>
* Whether to verify the R2 endpoint's SSL/TLS certificate.
* Default: true (correct for production environments).
* Set to false ONLY if your network performs TLS inspection (e.g. a Zero Trust
* proxy or corporate MITM gateway) and you cannot add the inspection CA to the
* Splunk server's trust store. Disabling SSL verification exposes the connection
* to man-in-the-middle attacks; prefer adding the CA cert instead.

key_prefix = <value>
* Optional key prefix to filter objects within the bucket.
* Use to scope one input to one Logpush dataset when multiple datasets
* share a bucket. Example: gateway_dns/ or http_requests/
* Leave empty to process all objects in the bucket.
* Default: (empty)

interval = <value>
* Polling interval in seconds. How often to check R2 for new log files.
* Default: 300 (5 minutes)

* Standard Splunk input fields (set per-input to route to correct index/sourcetype):
*
*   sourcetype = <value>
*     Recommended values for use with Cloudflare App for Splunk (Splunkbase 4501):
*       cloudflare:dns        - Zero Trust Gateway DNS logs
*       cloudflare:http       - Zero Trust Gateway HTTP logs
*       cloudflare:network    - Zero Trust Gateway Network logs
*       cloudflare:access     - Zero Trust Access logs
*       cloudflare:audit      - Cloudflare Audit logs
*       cloudflare:casb       - Zero Trust CASB logs
*       cloudflare:json       - Zone-level HTTP Request logs
*     These values are user-selected; the TA does not enforce any sourcetype.
*
*   index = <value>
*     Target Splunk index. Default: main
*
*   host = <value>
*     Host value for indexed events. Default: system hostname
