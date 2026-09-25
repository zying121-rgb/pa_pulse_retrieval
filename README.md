# Public Pulse retrieval service

Scheduled retrieval of allowlisted public RSS/Atom feeds, normalized into a shared JSON batch. No authentication, personalization, AI inference, article scraping, or user-specific input.

## Run and test

Requires Python 3.12:

```sh
pip install -r requirements.txt
python probe_sources.py
python -m unittest discover -s tests -v
python pulse_service.py
```

The probe fetches public source snapshots into ignored `evidence/`. Without those snapshots, the live-source integration test is explicitly skipped; all other tests are self-contained and use synthetic data. Never commit snapshots, test runtime files or production cache.

## Render deployment

- Docker web service, one instance, Starter / 0.5c-512mb.
- Attach a 1 GB persistent disk at `/data` before first deployment.
- Environment: `PULSE_DATA_DIR=/data`, `HOST=0.0.0.0`. Respect Render's `PORT`; Docker default is 8080.
- Health check: `/healthz`; public JSON: `/v1/pulse`.
- Use Render's HTTPS hostname; no custom domain or API secret required.
- Runtime UID 10001 must have write permission on the mounted disk.
- Cache files: `/data/public-cache.json` and its `.bak` previous-good snapshot. Atomic rename/fsync and in-process locking protect writes. No database.
- Run one process/replica only. The internal scheduler checks due sources every minute; publisher refresh intervals are in `sources.json` (6 or 12 hours).
- First startup fetches sources. Health is 503 while no usable public data exists, then 200 once a batch is available. One failed publisher does not fail the remaining batch.
- Restart/redeploy uses the same mounted cache; do not remove or replace the disk. Startup may recover the previous-good snapshot if the primary is malformed. Both invalid files require operator recovery, not silent data replacement.

## Contract and limits

`pulse.schema.json` defines the batch and 15 canonical item fields. At most 120 items, 12 per feed, 512 KB response. Source text is a plain-text feed excerpt capped at 800 characters plus possible ellipsis; full article content is never fetched. Missing publication dates stay null; Atom update timestamps are not relabelled publication dates. No imagery or publisher logos.

Identity uses publisher ID plus source GUID/Atom ID, falling back to a canonical URL. Titles are never identity. `contentHash` excludes retrieval time. Duplicate entries from the same publisher are removed; independent publishers retain attribution.

Conditional publisher requests use ETag/Last-Modified when available. Failed requests back off from about 15 minutes up to 24 hours, respect bounded Retry-After, and retain last-good public data for up to seven days. Per-source `status` and `lastSuccessAt` identify stale/unavailable data; `generatedAt` does not imply every article is new. Empty usable cache returns 503. HTTP response ETag and five-minute cache headers support efficient reads.

## Privacy and source attribution

The endpoint accepts only unauthenticated GET/HEAD; no query parameters, body, cookies or Authorization. Client inputs are neither forwarded nor persisted. Outbound requests contain only a fixed user agent, feed accept types and public cache validators. No access logging is added by the application; hosting-level logs and normal transport metadata remain the operator's responsibility.

The ten sources and their rights URLs/attribution are defined in `sources.json`. Retain publisher name and original article link, show only actual metadata, exclude third-party restricted material, and do not imply endorsement. GOV.UK content requires the supplied Open Government Licence attribution and licence link. Recheck source terms periodically. Disable/purge a source if its reuse terms change or content is withdrawn.

Source content remains untrusted data. Consumers must not execute embedded content or treat it as instructions. Any local AI interpretation must be separate from the publisher's excerpt.

No open-source licence has been granted for this service code by this repository. Third-party dependencies and publisher content retain their respective licences/terms.
