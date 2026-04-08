# BiomeClaw Production Reliability

This repo uses a GitHub-first production loop for the Hetzner deployment.

## Workflows

- `CI`: runs on every push and pull request. It runs Ruff, validates the production shell scripts, renders `docker compose` config, and runs the test suite.
- `Deploy Production`: manual `workflow_dispatch` only. It is restricted to `main`, verifies the selected ref is green, syncs the repo to Hetzner without overwriting the server `.env`, updates `APP_RELEASE` / `APP_DEPLOYED_AT`, restarts the stack, and runs smoke checks.
- `Monitor Production`: runs every 10 minutes and can also be triggered manually. It checks `/healthz`, `/readyz`, `/metrics`, `/api/admin/status`, plus `docker compose ps` and recent logs over SSH. Failures upload diagnostics, send a Telegram alert, and create or update a GitHub incident.

## Required GitHub Secrets

- `PROD_HOST`
- `PROD_USER`
- `PROD_APP_DIR`
- `PROD_SSH_KEY`
- `PROD_BASE_URL`
- `TELEGRAM_ALERT_BOT_TOKEN`
- `TELEGRAM_ALERT_CHAT_ID`

## Production Scripts

- `scripts/prod/deploy.sh`: safe rsync-based deploy. Server `.env` remains authoritative.
- `scripts/prod/smoke_check.sh`: endpoint and container health verification.
- `scripts/prod/collect_diagnostics.sh`: gathers endpoint responses, compose status, logs, and Docker state.
- `scripts/prod/notify_telegram.sh`: sends production alerts.
- `scripts/prod/report_incident.sh`: creates or updates GitHub production incidents.

## Runtime Observability

- Health APIs now return `X-Request-ID` headers.
- Structured 5xx responses include `requestId` and `errorId`.
- `/readyz` reports dependency readiness.
- `/metrics` includes release metadata, readiness, request counters, and the latest production error snapshot.
- Fragile onboarding/setup paths log structured errors with request references.

## Local Commands

```bash
bash scripts/prod/deploy.sh --host 46.62.231.14 --user root --app-dir /opt/biomeclaw --base-url http://46.62.231.14
```

```bash
bash scripts/prod/smoke_check.sh --host 46.62.231.14 --user root --app-dir /opt/biomeclaw --base-url http://46.62.231.14
```

```bash
bash scripts/prod/collect_diagnostics.sh --host 46.62.231.14 --user root --app-dir /opt/biomeclaw --base-url http://46.62.231.14
```
