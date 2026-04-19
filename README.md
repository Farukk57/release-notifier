# release-notifier

![Docker Pulls](https://img.shields.io/docker/pulls/57faruk57/release-notifier)
![Docker Image Size](https://img.shields.io/docker/image-size/57faruk57/release-notifier/latest)

Watches GitHub releases for your self-hosted Docker stack, summarises changelogs with Claude, OpenAI, or a local Ollama model, and pushes notifications to your phone via **ntfy** and/or **Home Assistant**. Also exposes a small API and dashboard for Homepage integration.

## Features

- Checks GitHub releases on a configurable schedule
- AI-powered summaries via Claude, OpenAI (gpt-4o-mini / gpt-4o), or local Ollama
- Urgency classification — urgent for CVEs, high for breaking changes, default otherwise
- Registry digest tracking — knows whether you've already pulled the new image or not
- OCI label version detection — shows `v1.0.0 -> v1.1.0` in notification titles
- Pre-release filtering — ignores nightlies and RCs by default
- Push notifications via ntfy (Android + iOS)
- Home Assistant integration — mobile push + persistent notification history in the HA bell
- Critical alert support — urgent releases bypass Do Not Disturb on iOS via HA
- Deduplication — won't re-notify for versions already sent
- Retry with exponential backoff on transient failures
- Homepage iframe dashboard widget
- Production-ready: waitress WSGI, non-root container, XSS protection, optional API key auth

## Quick start

### 1. Install the ntfy app

- Android: https://play.google.com/store/apps/details?id=io.heckel.ntfy
- iOS: https://apps.apple.com/app/ntfy/id1625396347

Subscribe to a topic, e.g. `ntfy.sh/my-homelab-updates`.

### 2. Get an AI API key (or use Ollama)

- **Claude:** sign up at https://console.anthropic.com
- **OpenAI:** sign up at https://platform.openai.com/api-keys
- **Ollama:** see [Using Ollama](#using-ollama-free-local-ai) to run locally for free

### 3. Run with Docker

```bash
mkdir -p release-notifier/config release-notifier/data
cd release-notifier

# Download example files
curl -O https://raw.githubusercontent.com/Farukk57/release-notifier/main/docker-compose.hub.yml
curl -O https://raw.githubusercontent.com/Farukk57/release-notifier/main/env.example
curl -O https://raw.githubusercontent.com/Farukk57/release-notifier/main/config/containers.yml.example

cp env.example .env
cp config/containers.yml.example config/containers.yml

# Edit both files
nano .env
nano config/containers.yml

docker compose -f docker-compose.hub.yml up -d
```

### 4. Verify

```bash
docker logs release-notifier
curl http://localhost:8080/api/health
```

## Docker Compose example

```yaml
services:
  release-notifier:
    image: 57faruk57/release-notifier:latest
    container_name: release-notifier
    restart: unless-stopped
    volumes:
      - ./config:/config:ro
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - ANTHROPIC_API_KEY=your_key
      - NTFY_URL=https://ntfy.sh/your-topic
      - GITHUB_TOKEN=your_github_pat
      - API_KEY=your_secret
    ports:
      - "8080:8080"
```

> **Traefik users:** remove the `ports` block and add your Traefik labels instead.

## Configuration

### Environment variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes* | Claude API key from console.anthropic.com |
| `NTFY_URL` | Yes** | ntfy topic URL, e.g. `https://ntfy.sh/my-topic` |
| `GITHUB_TOKEN` | Recommended | GitHub PAT (no scopes needed) — avoids 60 req/h rate limit |
| `NTFY_TOKEN` | Optional | ntfy access token for private topics |
| `API_KEY` | Optional | Protects `POST /api/check` from unauthenticated triggers |
| `AI_PROVIDER` | Optional | `claude` (default), `openai`, or `ollama` |
| `OPENAI_API_KEY` | Optional | OpenAI API key from platform.openai.com |
| `OPENAI_MODEL` | Optional | `gpt-4o-mini` (default) or `gpt-4o` |
| `OLLAMA_URL` | Optional | Ollama base URL, e.g. `http://192.168.1.10:11434` |
| `OLLAMA_MODEL` | Optional | Ollama model name, e.g. `mistral` |
| `NOTIFY_PROVIDER` | Optional | `ntfy` (default), `homeassistant`, or `both` |
| `HASS_URL` | Optional | Home Assistant URL, e.g. `http://homeassistant.local:8123` |
| `HASS_TOKEN` | Optional | Long-lived access token from your HA profile |
| `HASS_NOTIFY_SERVICE` | Optional | Notify service name, e.g. `mobile_app_your_phone` |

*Not required if using OpenAI or Ollama.
**Not required if using Home Assistant only.

### containers.yml

See `config/containers.yml.example` for a full annotated example.

Each container entry supports:

```yaml
- name: MyApp           # display name in notifications
  github: owner/repo    # GitHub repo for release tracking
  image: ghcr.io/...    # Docker image for digest tracking (optional)
  container: myapp      # actual Docker container name if different from name (optional)
  stable_only: true     # ignore pre-releases (default: true)
```

## Home Assistant integration

Send rich push notifications directly to your phone via the HA companion app, with a persistent notification history in the HA bell icon.

### Setup

**1. Create a long-lived access token in HA:**

Go to your HA profile → **Security** → **Long-lived access tokens** → create one called `release-notifier`.

**2. Find your notify service name:**

Go to **Developer tools** → **Actions** → search `notify.mobile_app` — your phone appears as something like `notify.mobile_app_your_phone`. Use the part after `notify.`.

**3. Configure:**

```bash
# .env
NOTIFY_PROVIDER=both
HASS_URL=http://homeassistant.local:8123
HASS_TOKEN=your_long_lived_token
HASS_NOTIFY_SERVICE=mobile_app_your_phone
```

### What you get

- Mobile push notification on your phone
- Persistent notification in the HA bell (stays until dismissed, browsable history)
- **Urgent** releases → critical alert bypassing Do Not Disturb on iOS
- **High** releases → high importance notification
- Tap notification to open the GitHub release page

### Critical alerts on iOS

For urgent releases (CVEs) to bypass silent mode, enable Critical Alerts for the HA app:

**Settings → Notifications → Home → Critical Alerts → On**

## Using OpenAI

Use GPT-4o-mini or GPT-4o instead of Claude. OpenAI is significantly cheaper than Claude Sonnet (~$0.15/1M tokens vs ~$3/1M) while producing comparable quality summaries.

```bash
# .env
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini   # default — cheap and fast
# OPENAI_MODEL=gpt-4o      # higher quality, ~17x more expensive
```

### Model comparison

| Model | Quality | Speed | Cost (per 1M tokens) |
|---|---|---|---|
| Claude Sonnet | Best | Fast | ~$3.00 |
| `gpt-4o` | Best | Medium | ~$2.50 |
| `gpt-4o-mini` | Good | Fast | ~$0.15 |
| Ollama mistral | Good | Slow (local) | Free |

## Using Ollama (free, local AI)

Run summaries locally with [Ollama](https://ollama.com) — no API key needed.

```bash
# Install and start Ollama (listen on all interfaces for Docker access)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull mistral
OLLAMA_HOST=0.0.0.0 ollama serve

# Open firewall if needed (Linux)
sudo firewall-cmd --add-port=11434/tcp --permanent && sudo firewall-cmd --reload
```

```bash
# .env
AI_PROVIDER=ollama
OLLAMA_URL=http://192.168.1.10:11434
OLLAMA_MODEL=mistral
```

### Recommended models

| Model | Size | Quality | Speed |
|---|---|---|---|
| `mistral` | ~4GB | Best | Medium |
| `llama3.2` | ~2GB | Good | Fast |
| `llama3.1:8b` | ~5GB | Best | Slow |

> Note: Ollama responses take 10–60 seconds per summary vs ~3 seconds with Claude.

## API

| Endpoint | Description |
|---|---|
| `GET /api/health` | Status, last/next check time, AI provider, docker socket |
| `GET /api/updates` | Last 10 detected updates as JSON |
| `GET /api/latest` | Most recent update (for Homepage customapi widget) |
| `POST /api/check` | Trigger immediate check (requires X-API-Key if API_KEY is set) |
| `GET /dashboard` | HTML dashboard (for Homepage iframe widget) |

## Homepage integration

```yaml
# services.yaml
- Release Notifier:
    icon: mdi-bell-ring
    href: https://releases.yourdomain.com
    widget:
      type: iframe
      src: http://release-notifier:8080/dashboard
      classes: h-96
```

## Switching providers

All providers can be switched by editing `.env` and restarting the container. No rebuild needed.

### AI provider

```bash
# Claude (default)
AI_PROVIDER=claude

# OpenAI
AI_PROVIDER=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini

# Ollama (free, local)
AI_PROVIDER=ollama
OLLAMA_URL=http://192.168.1.10:11434
OLLAMA_MODEL=mistral
```

### Notification provider

```bash
# ntfy only (default)
NOTIFY_PROVIDER=ntfy

# Home Assistant only
NOTIFY_PROVIDER=homeassistant

# Both simultaneously
NOTIFY_PROVIDER=both
```

After any `.env` change, restart the container:

```bash
docker compose up -d
```

Check which providers are active:

```bash
curl http://localhost:8080/api/health
# Shows: "ai_provider": "Claude", "notify_provider": "both"
```

## Triggering a manual check

```bash
# Without API key
curl -X POST http://localhost:8080/api/check -H "Content-Length: 0"

# With API key set
curl -X POST http://localhost:8080/api/check \
  -H "X-API-Key: your_key" \
  -H "Content-Length: 0"
```

## License

MIT — see [LICENSE](LICENSE) for details.

## Credits

Built with the assistance of [Claude](https://claude.ai) by Anthropic.
