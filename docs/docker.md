# FigWatch — Docker / Server Deployment

FigWatch runs as a headless webhook server — no macOS required. Rather than polling Figma on a timer, it receives events from Figma in real time and processes them immediately.

## What you'll need

Setup involves two roles that are often different people — a **Figma admin** who can generate tokens and register webhooks, and a **server operator** who runs Docker. Collect everything below before starting.

### From your Figma admin

| What | Where to get it | Used for |
|------|----------------|---------|
| **Figma Personal Access Token** | Figma → Settings → Security → [Personal access tokens](https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens) | Authenticating API requests |
| **Figma team ID** | From your team URL: `figma.com/files/team/`**`1234567890`**`/…` — the number after `/team/` | Registering the webhook |

> The Figma account providing the token must be on a **Professional or Organisation plan** — Figma webhooks are not available on Starter (free) accounts.

### AI provider key — choose one

| Provider | Where to get it | Cost |
|----------|----------------|------|
| **Google AI (Gemini)** — recommended | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Free tier available |
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com/) | Paid |

### From your server operator

| What | Notes |
|------|-------|
| **Server URL** | A publicly accessible HTTPS URL where Figma can send events. Use [ngrok](https://ngrok.com) for local testing. |
| **Webhook passcode** | Any secret string you choose — used to verify that webhook events genuinely come from Figma. |

---

## How it works

Figma sends a `FILE_COMMENT` webhook event to your server whenever a comment is posted in your team. FigWatch checks whether the comment contains a trigger word (`@ux`, `@tone`, etc.), fetches the frame data from Figma, runs the AI audit, and posts the result as a reply — all within the same comment thread.

## Quick start

### 1. Configure environment

```bash
cp .env.example .env
```

Fill in your values. Minimum required:

```env
FIGMA_PAT=figd_your_token_here
FIGWATCH_WEBHOOK_PASSCODE=choose-a-secret-passphrase
FIGWATCH_TEAM_ID=1234567890
GOOGLE_API_KEY=your_google_ai_key_here
```

> **Note:** `DATABASE_URL` is set automatically by `docker-compose.yml` — you don't need to add it to `.env` unless overriding the bundled PostgreSQL instance.

### 2. Start the server

```bash
docker compose up -d --build
```

This starts two containers:
- **PostgreSQL 18** — stores the work queue, processed comment deduplication, and audit history
- **FigWatch** — the webhook server (waits for PostgreSQL to be healthy before starting)

Database migrations are applied automatically on startup. No manual schema setup required.

The server listens on port `8080` and exposes three endpoints:
- `POST /webhook` — receives Figma webhook events
- `GET /health` — returns `ok` (used by Docker healthcheck)
- `GET /audits` — query audit history (see [Audit history](#audit-history) below)

### 3. Expose the server to the internet

Figma's servers need to be able to reach your endpoint over HTTPS.

**Production:** point your domain at the server, terminate TLS with a reverse proxy (nginx, Caddy, etc.).

**Local development:** use [ngrok](https://ngrok.com):

```bash
ngrok http 8080
```

This gives you a URL like `https://your-subdomain.ngrok-free.app`. Copy it — you need it in the next step.

### 4. Register the webhook with Figma

Run this curl command once. It reads `FIGMA_PAT`, `FIGWATCH_WEBHOOK_PASSCODE`, and `FIGWATCH_TEAM_ID` from your `.env` — just replace `YOUR_HOST`:

```bash
source .env

curl -X POST https://api.figma.com/v2/webhooks \
  -H "X-Figma-Token: $FIGMA_PAT" \
  -H "Content-Type: application/json" \
  -d "{
    \"event_type\": \"FILE_COMMENT\",
    \"team_id\": \"$FIGWATCH_TEAM_ID\",
    \"endpoint\": \"https://YOUR_HOST/webhook\",
    \"passcode\": \"$FIGWATCH_WEBHOOK_PASSCODE\"
  }"
```

For full webhook API details, see the [Figma Webhooks documentation](https://www.figma.com/developers/api#webhooks_v2).

Figma will immediately send a `PING` event to verify the endpoint is reachable. Check your logs:

```bash
docker compose logs -f
```

You should see something like:

```
2026-04-17 12:00:00 INFO  __main__     🏓 ping received
```

If you see a 403 instead, the passcode in the request doesn't match `FIGWATCH_WEBHOOK_PASSCODE` in your `.env`.

## Using FigWatch

Pin a comment containing a trigger word to a frame in Figma:

1. Press **C** to activate the comment tool
2. **Click directly on a frame** — the frame should highlight before you click
3. Type `@ux` (or `@tone`) and post the comment

> **The comment must be pinned to a frame.** Floating canvas comments have no node ID and will be skipped. The cursor must be over a specific frame when you click, not on empty canvas.

Within seconds you should see the audit appear as a reply in the same thread.

## Environment variables

All variables are documented in [`.env.example`](../.env.example) with sensible defaults. The tables below group them by function.

### Required

| Variable | Description |
|----------|-------------|
| `FIGMA_PAT` | Figma Personal Access Token |
| `FIGWATCH_WEBHOOK_PASSCODE` | Secret passphrase set when registering the webhook |
| `FIGWATCH_TEAM_ID` | Figma team ID — needed for webhook registration |
| `DATABASE_URL` | PostgreSQL connection string (e.g. `postgresql://user:pass@host:5432/dbname`). Set automatically by `docker-compose.yml` — only needed when using an external database. |
| `GOOGLE_API_KEY` | Google AI API key — required when `FIGWATCH_MODEL` starts with `gemini` |
| `ANTHROPIC_API_KEY` | Anthropic API key — required when `FIGWATCH_MODEL` is `sonnet`, `opus`, or `haiku` |

> You need at least one AI provider key. Both can be set — the one used depends on `FIGWATCH_MODEL`.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `FIGWATCH_MODEL` | `gemini-flash` | `gemini-flash`, `gemini-flash-lite`, `sonnet`, `opus`, or `haiku` |
| `FIGWATCH_FILES` | — | Comma-separated Figma file URLs or keys. Unset = all team files |
| `FIGWATCH_LOCALE` | `uk` | Locale for tone audits: `uk`, `de`, `fr`, `nl`, `benelux` |
| `FIGWATCH_PORT` | `8080` | Port to listen on |
| `FIGWATCH_WORKERS` | `4` | Concurrent skill executions |

### Rate limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `FIGWATCH_GEMINI_RPM` | `15` | Gemini requests-per-minute cap. Workers block locally when the limit is reached. `0` to disable. |
| `FIGWATCH_ANTHROPIC_RPM` | `5` | Anthropic requests-per-minute cap. `0` to disable. |
| `FIGWATCH_QUEUE_UPDATE_RPM` | `5` | Live queue-position ack updates per minute. `0` to disable — acks stay at their initial position until picked up. |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `FIGWATCH_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `FIGWATCH_LOG_FORMAT` | `text` | `text` (human-readable, ANSI colors in TTY) or `json` (one object per line, for Loki/Datadog) |

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OpenTelemetry collector endpoint (e.g. `http://otel-collector:4317`). Metrics disabled when unset. |

## Custom skills

Mount a directory of custom skill files into the container:

```yaml
# docker-compose.yml (already configured)
volumes:
  - ./custom-skills:/app/custom-skills
```

Place `.md` skill files in `./custom-skills/` on the host. FigWatch registers each file as a trigger based on its filename — `a11y.md` becomes `@a11y`, `brand.md` becomes `@brand`. No additional configuration required.

## Viewing logs

```bash
docker compose logs -f
```

A healthy run looks like this:

```
2026-04-14 19:19:06 INFO  __main__     file=abc123 comment=1234567 📥 webhook received
2026-04-14 19:19:06 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 user=alice 💬 trigger matched
2026-04-14 19:19:06 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 depth=1 queue.enqueued
2026-04-14 19:19:06 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 depth=0 waited=0.10s queue.dequeued
2026-04-14 19:19:06 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 skill=builtin:ux running skill
2026-04-14 19:19:08 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 chars=1842 skill returned
2026-04-14 19:19:08 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 reply_to=1234567 reply posted
2026-04-14 19:19:08 INFO  __main__     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 queued=0.10s running=2.00s total=2.10s attempts=1 ✅ audit.completed
```

### Correlating log lines for a single audit

Every log line produced while processing a comment carries the same `audit=XXXXXXXX` ID. To see the full lifecycle for one audit in [Dozzle](https://dozzle.dev) or `docker compose logs`, search for that audit ID:

```bash
docker compose logs figwatch | grep 'audit=a3f9e2d1'
```

The same works for `trigger=@ux`, `node=176:24454`, or `file=abc123` if you want to filter by other dimensions.

## OpenTelemetry metrics

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to export metrics to any OTel-compatible collector:

```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

Key metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `figwatch.webhook.received_total` | Counter | Webhook events received, by `event_type` |
| `figwatch.webhook.last_received_seconds` | Gauge | Unix timestamp of last webhook event |
| `figwatch.audit.duration_seconds` | Histogram | End-to-end audit time |
| `figwatch.audit.total` | Counter | Audits completed, by `status` |
| `figwatch.queue.depth` | UpDownCounter | Current queue depth |

Metrics are disabled (zero overhead) when `OTEL_EXPORTER_OTLP_ENDPOINT` is not set.

## Audit history

FigWatch stores completed and failed audits in PostgreSQL for 7 days. Query them via the `/audits` endpoint:

```bash
# All recent audits
curl http://localhost:8080/audits

# Filter by user
curl http://localhost:8080/audits?user=alice

# Filter by status
curl http://localhost:8080/audits?status=completed

# Combine filters
curl http://localhost:8080/audits?user=bob&status=failed&limit=10
```

Each audit includes the OTel `trace_id` for correlating with your tracing backend (Grafana, Jaeger, etc.):

```json
{
  "audit_id": "a3f9e2d1",
  "status": "completed",
  "user_handle": "alice",
  "trigger_keyword": "@ux",
  "file_key": "abc123",
  "enqueued_at": "2026-07-21T14:30:00+00:00",
  "completed_at": "2026-07-21T14:30:45+00:00",
  "attempt": 1,
  "trace_id": "0af7651916cd43dd8448eb211c80319c"
}
```

You can also query the database directly:

```sql
-- Audits per user in the last 24 hours
SELECT user_handle, count(*) AS total,
       count(*) FILTER (WHERE status = 'completed') AS succeeded
FROM audit_queue
WHERE enqueued_at > now() - interval '1 day'
GROUP BY user_handle
ORDER BY total DESC;
```

## Troubleshooting

**`skip: no node_id`**
The comment was not pinned to a frame. In Figma, press C, hover over a frame until it highlights, then click and post your trigger comment. Floating canvas comments have no associated node.

**`skip: no trigger`**
The comment text doesn't contain a recognised trigger word. The server logs show which triggers are active at startup.

**`skip: file not in allowlist`**
`FIGWATCH_FILES` is set and the comment came from a different file. Either add the file to the allowlist or clear `FIGWATCH_FILES` to handle all team files.

**`skip: already processed`**
Figma retries webhook delivery if your server doesn't respond quickly enough. FigWatch deduplicates by comment ID in PostgreSQL, so this is harmless.

**`403 Forbidden` on webhook delivery**
The passcode in the registered webhook doesn't match `FIGWATCH_WEBHOOK_PASSCODE`. Re-register the webhook (see below) with the correct passcode.

**Gemini 429 — quota exceeded**
The free tier has a token-per-minute limit. FigWatch retries once after the suggested delay. If you hit this regularly, consider upgrading to a paid Google AI tier or switching to `FIGWATCH_MODEL=sonnet` with an Anthropic key.

**No webhook events arriving**
- Check that your endpoint is publicly reachable over HTTPS — Figma requires HTTPS
- Verify the webhook is registered: `curl https://api.figma.com/v2/teams/$FIGWATCH_TEAM_ID/webhooks -H "X-Figma-Token: $FIGMA_PAT"`
- The PAT used to register the webhook must belong to an account that has access to the team on a paid plan
- If using ngrok, make sure the tunnel is still running — free ngrok URLs expire on restart

**Audit takes a long time / times out**
- Check `FIGWATCH_LOG_LEVEL=DEBUG` to see where time is spent
- Large frames produce big screenshots — the progressive fallback may need multiple attempts
- Gemini free tier has a tokens-per-minute limit that can cause queuing under load

**Container exits immediately**
- Check logs: `docker compose logs figwatch`
- Most common cause: missing required env vars (`FIGMA_PAT`, `FIGWATCH_WEBHOOK_PASSCODE`, `DATABASE_URL`, AI key)
- If `database connection failed`: ensure PostgreSQL is running and reachable at the `DATABASE_URL`

## Example production deployment

[figwatch-olivia](https://github.com/simonpforster/figwatch-olivia) is a complete deployment stack that adds:

- **Cloudflare Tunnel** — HTTPS ingress without a reverse proxy or public IP
- **OpenTelemetry Collector** — receives metrics from FigWatch and forwards to Prometheus
- **Prometheus** — stores metrics with 30-day retention
- **Grafana** — dashboards for audit duration, queue depth, and webhook reliability

Clone it as a starting point for your own production setup.

## Managing webhooks

List your registered webhooks:

```bash
curl https://api.figma.com/v2/teams/$FIGWATCH_TEAM_ID/webhooks \
  -H "X-Figma-Token: $FIGMA_PAT"
```

Delete a webhook (use the `id` from the list response):

```bash
curl -X DELETE https://api.figma.com/v2/webhooks/WEBHOOK_ID \
  -H "X-Figma-Token: $FIGMA_PAT"
```

## Stopping

```bash
docker compose down
```

PostgreSQL data is stored in the `pgdata` Docker volume and persists across restarts. To fully reset the database:

```bash
docker compose down -v
```
