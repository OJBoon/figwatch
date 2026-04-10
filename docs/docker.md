# FigWatch — Docker / Server Deployment

FigWatch can run as a headless server — no macOS required. This is useful for teams who want a shared, always-on instance rather than running the app on one person's machine.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Docker Compose
- A [Figma Personal Access Token](https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens)
- An [Anthropic API key](https://console.anthropic.com/)

Claude Code is bundled inside the container — you do not need it installed on the host machine.

## Quick start

1. Copy `.env.example` to `.env` and fill in your values:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env`:

   ```env
   FIGMA_PAT=figd_your_token_here
   FIGWATCH_FILES=https://www.figma.com/design/yourFileKey/your-file-name
   ANTHROPIC_API_KEY=sk-ant-your_key_here
   ```

3. Start the server:

   ```bash
   docker compose up -d --build
   ```

FigWatch will start polling your Figma files immediately. Comments with trigger keywords (e.g. `@tone`, `@ux`) will be picked up and replied to automatically.

## Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FIGMA_PAT` | Yes | — | Figma Personal Access Token |
| `FIGWATCH_FILES` | Yes | — | Comma-separated Figma file URLs or bare file keys |
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key, passed to Claude |
| `FIGWATCH_LOCALE` | No | `uk` | Default locale for tone audits: `uk`, `de`, `fr`, `nl`, `benelux` |
| `FIGWATCH_MODEL` | No | `sonnet` | Claude model: `sonnet`, `opus`, `haiku` |
| `FIGWATCH_INTERVAL` | No | `30` | How often to poll Figma for new comments (seconds) |

### Watching multiple files

Set `FIGWATCH_FILES` to a comma-separated list of URLs or file keys:

```env
FIGWATCH_FILES=https://www.figma.com/design/abc123/File-One,https://www.figma.com/design/def456/File-Two
```

FigWatch staggers poll starts across files to avoid hitting the Figma API simultaneously.

## Custom skills

Mount a directory of custom skill files into the container:

```yaml
# docker-compose.yml (already configured)
volumes:
  - ./custom-skills:/app/custom-skills
```

Place your `.md` skill files in `./custom-skills/` on the host. FigWatch will pick them up automatically — no restart required.

## Viewing logs

```bash
docker compose logs -f
```

## Stopping

```bash
docker compose down
```
