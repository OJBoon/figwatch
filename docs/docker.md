# FigWatch — Docker / Server Deployment

FigWatch runs as a headless webhook server — no macOS required. Rather than polling Figma on a timer, it receives events from Figma in real time and processes them immediately.

## What you'll need

Setup involves two roles that are often different people — a **Figma admin** who can generate tokens and register webhooks, and a **server operator** who runs Docker. Collect everything below before starting.

### From your Figma admin

| What | Where to get it | Used for |
|------|----------------|---------|
| **Figma Personal Access Token** | Figma → Settings → Security → [Personal access tokens](https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens) | Authenticating API requests |
| **Figma team ID** | Figma URL when browsing your team: `figma.com/files/team/`**`1234567890`**`/…` | Registering the webhook |

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
GOOGLE_API_KEY=your_google_ai_key_here
```

### 2. Start the server

```bash
docker compose up -d --build
```

The server listens on port `8080` and exposes two endpoints:
- `POST /webhook` — receives Figma webhook events
- `GET /health` — returns `ok` (used by Docker healthcheck)

### 3. Expose the server to the internet

Figma's servers need to be able to reach your endpoint over HTTPS.

**Production:** point your domain at the server, terminate TLS with a reverse proxy (nginx, Caddy, etc.).

**Local development:** use [ngrok](https://ngrok.com):

```bash
ngrok http 8080
```

This gives you a URL like `https://your-subdomain.ngrok-free.app`. Copy it — you need it in the next step.

### 4. Find your Figma team ID

Open Figma and browse to your team's files. The URL looks like:

```
https://www.figma.com/files/team/1234567890/your-team-name
```

The number after `/team/` is your team ID.

> **Professional or Organisation plan required.** Figma webhooks are only available on paid plans. Starter (free) accounts cannot register webhooks.

### 5. Register the webhook with Figma

Run this curl command once. Replace the placeholders with your actual values:

```bash
curl -X POST https://api.figma.com/v2/webhooks \
  -H "X-Figma-Token: $FIGMA_PAT" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "FILE_COMMENT",
    "team_id": "YOUR_TEAM_ID",
    "endpoint": "https://YOUR_HOST/webhook",
    "passcode": "YOUR_FIGWATCH_WEBHOOK_PASSCODE"
  }'
```

Figma will immediately send a `PING` event to verify the endpoint is reachable. Check your logs:

```bash
docker compose logs -f
```

You should see:

```
→ POST /webhook 200
```

If you see a 403, the passcode in the request doesn't match `FIGWATCH_WEBHOOK_PASSCODE` in your `.env`.

## Using FigWatch

Pin a comment containing a trigger word to a frame in Figma:

1. Press **C** to activate the comment tool
2. **Click directly on a frame** — the frame should highlight before you click
3. Type `@ux` (or `@tone`) and post the comment

> **The comment must be pinned to a frame.** Floating canvas comments have no node ID and will be skipped. The cursor must be over a specific frame when you click, not on empty canvas.

Within seconds you should see the audit appear as a reply in the same thread.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FIGMA_PAT` | Yes | — | Figma Personal Access Token |
| `FIGWATCH_WEBHOOK_PASSCODE` | Yes | — | Secret passphrase set when registering the webhook |
| `GOOGLE_API_KEY` | One of these | — | Google AI API key — used when `FIGWATCH_MODEL` starts with `gemini` |
| `ANTHROPIC_API_KEY` | One of these | — | Anthropic API key — used when `FIGWATCH_MODEL` is `sonnet`, `opus`, or `haiku` |
| `FIGWATCH_MODEL` | No | `gemini-flash` | Model: `gemini-flash`, `gemini-flash-lite` (Gemini) or `sonnet`, `opus`, `haiku` (Claude) |
| `FIGWATCH_FILES` | No | — | Comma-separated Figma file URLs or keys. If unset, handles comments from all team files |
| `FIGWATCH_LOCALE` | No | `uk` | Locale for tone audits: `uk`, `de`, `fr`, `nl`, `benelux` |
| `FIGWATCH_PORT` | No | `8080` | Port to listen on |
| `FIGWATCH_WORKERS` | No | `4` | Number of concurrent skill executions |
| `FIGWATCH_MAX_ATTEMPTS` | No | `3` | Retry attempts per audit before giving up (backoff: 30s, 2m, 5m) |
| `FIGWATCH_GEMINI_RPM` | No | `15` | Gemini requests-per-minute cap. Workers block locally when the limit is reached rather than hitting 429s. Set to `0` to disable. |
| `FIGWATCH_ANTHROPIC_RPM` | No | `5` | Anthropic requests-per-minute cap. Set to `0` to disable. |
| `FIGWATCH_LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. `DEBUG` shows ack lifecycle, Figma API calls, rate limiter acquires. |
| `FIGWATCH_LOG_FORMAT` | No | `text` | `text` for human-readable output (Dozzle-friendly, ANSI colors in TTY), or `json` for one JSON object per line (for log aggregators like Loki, Datadog). |

## Restricting to specific files

By default FigWatch handles comments from all files in your Figma team. To restrict it:

```env
FIGWATCH_FILES=https://www.figma.com/design/abc123/File-One,https://www.figma.com/design/def456/File-Two
```

You can mix full URLs and bare file keys.

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
2026-04-14 19:19:06 INFO  server     📥 webhook received file=abc123 comment=1234567
2026-04-14 19:19:06 INFO  server     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 💬 trigger matched user=alice
2026-04-14 19:19:06 INFO  server     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 queue.enqueued depth=1
2026-04-14 19:19:06 INFO  server     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 queue.dequeued depth=0 waited=0.10s
2026-04-14 19:19:06 INFO  skills     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 running skill skill=builtin:ux
2026-04-14 19:19:08 INFO  skills     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 skill returned chars=1842
2026-04-14 19:19:08 INFO  server     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 reply posted reply_to=1234567
2026-04-14 19:19:08 INFO  server     audit=a3f9e2d1 trigger=@ux node=176:24454 file=abc123 attempt=1 ✅ audit.completed queued=0.10s running=2.00s total=2.10s attempts=1

```

### Correlating log lines for a single audit

Every log line produced while processing a comment carries the same `audit=XXXXXXXX` ID. To see the full lifecycle for one audit in [Dozzle](https://dozzle.dev) or `docker compose logs`, search for that audit ID:

```bash
docker compose logs figwatch | grep 'audit=a3f9e2d1'
```

The same works for `trigger=@ux`, `node=176:24454`, or `file=abc123` if you want to filter by other dimensions.

## Troubleshooting

**`skip: no node_id`**
The comment was not pinned to a frame. In Figma, press C, hover over a frame until it highlights, then click and post your trigger comment. Floating canvas comments have no associated node.

**`skip: no trigger`**
The comment text doesn't contain a recognised trigger word. The server logs show which triggers are active at startup.

**`skip: file not in allowlist`**
`FIGWATCH_FILES` is set and the comment came from a different file. Either add the file to the allowlist or clear `FIGWATCH_FILES` to handle all team files.

**`skip: already processed`**
Figma retries webhook delivery if your server doesn't respond quickly enough. FigWatch deduplicates by comment ID, so this is harmless.

**`403 Forbidden` on webhook delivery**
The passcode in the registered webhook doesn't match `FIGWATCH_WEBHOOK_PASSCODE`. Re-register the webhook (see below) with the correct passcode.

**Gemini 429 — quota exceeded**
The free tier has a token-per-minute limit. FigWatch retries once after the suggested delay. If you hit this regularly, consider upgrading to a paid Google AI tier or switching to `FIGWATCH_MODEL=sonnet` with an Anthropic key.

**No webhook events arriving**
- Check that your endpoint is publicly reachable over HTTPS — Figma requires HTTPS
- Verify the webhook is registered: `curl https://api.figma.com/v2/teams/YOUR_TEAM_ID/webhooks -H "X-Figma-Token: $FIGMA_PAT"`
- The PAT used to register the webhook must belong to an account that has access to the team on a paid plan

## Managing webhooks

List your registered webhooks:

```bash
curl https://api.figma.com/v2/teams/YOUR_TEAM_ID/webhooks \
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
