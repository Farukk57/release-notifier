# release-notifier

![Docker Pulls](https://img.shields.io/docker/pulls/57faruk57/release-notifier)
![Docker Image Size](https://img.shields.io/docker/image-size/57faruk57/release-notifier/latest)

Watches GitHub releases for your self-hosted Docker stack, summarises changelogs with Claude AI or a local Ollama model, and pushes a push notification to your phone via **ntfy**. Also exposes a small API and dashboard for Homepage integration.

## Features

- Checks GitHub releases on a configurable schedule
- AI-powered summaries via Claude or local Ollama (what changed, security patches, action required)
- Urgency classification — urgent for CVEs, high for breaking changes, default otherwise
- Registry digest tracking — knows whether you've already pulled the new image or not
- OCI label version detection — shows `v1.0.0 -> v1.1.0` in notification titles
- Pre-release filtering — ignores nightlies and RCs by default
- Push notifications via ntfy (Android + iOS)
- Deduplication — won't re-notify for versions already sent
- Retry with exponential backoff on transient failures
- Homepage iframe dashboard widget
- Production-ready: waitress WSGI, non-root container, XSS protection, optional API key auth

## Quick start

### 1. Install the ntfy app

- Android: https://play.google.com/store/apps/details?id=io.heckel.ntfy
- iOS: https://apps.apple.com/app/ntfy/id1625396347

Subscribe to a topic, e.g. `ntfy.sh/my-homelab-updates`.

### 2. Get an Anthropic API key (or use Ollama)

Sign up at https://console.anthropic.com and create an API key.

Alternatively, see [Using Ollama](#using-ollama-free-local-ai) to run summaries locally for free.

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
      - ./config:/config:ro       # contains containers.yml
      - ./data:/data               # state and update history
      - /var/run/docker.sock:/var/run/docker.sock:ro  # for version tracking
    environment:
      - ANTHROPIC_API_KEY=your_key
      - NTFY_URL=https://ntfy.sh/your-topic
      - GITHUB_TOKEN=your_github_pat   # optional but recommended
      - API_KEY=your_secret            # optional, protects /api/check
    ports:
      - "8080:8080"
```

> **Traefik users:** remove the `ports` block and add your Traefik labels instead. The container exposes port `8080` internally.

## Configuration

### Environment variables (`.env`)

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes* | Claude API key from console.anthropic.com |
| `NTFY_URL` | Yes | ntfy topic URL, e.g. `https://ntfy.sh/my-topic` |
| `GITHUB_TOKEN` | Recommended | GitHub PAT (no scopes needed) — avoids 60 req/h rate limit |
| `NTFY_TOKEN` | Optional | ntfy access token for private topics |
| `API_KEY` | Optional | Protects `POST /api/check` from unauthenticated triggers |
| `AI_PROVIDER` | Optional | `claude` (default) or `ollama` |
| `OLLAMA_URL` | Optional | Ollama base URL, e.g. `http://192.168.1.10:11434` |
| `OLLAMA_MODEL` | Optional | Ollama model name, e.g. `mistral` |

*Not required if using Ollama.

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

## Using Ollama (free, local AI)

If you don't want to pay for API credits, you can run summaries locally with [Ollama](https://ollama.com). No internet connection required for the AI step.

### Setup

Install Ollama on a machine on your local network:

```bash
# Install
curl -fsSL https://ollama.com/install.sh | sh

# Pull a model
ollama pull mistral          # recommended — best quality for this use case
# or
ollama pull llama3.2         # smaller and faster (~2GB)

# Start with network access (required so Docker containers can reach it)
OLLAMA_HOST=0.0.0.0 ollama serve
```

If your firewall blocks the port:
```bash
sudo firewall-cmd --add-port=11434/tcp --permanent
sudo firewall-cmd --reload
```

### Configure release-notifier

```bash
# .env
AI_PROVIDER=ollama
OLLAMA_URL=http://192.168.1.10:11434   # IP of the machine running Ollama
OLLAMA_MODEL=mistral
```

No `ANTHROPIC_API_KEY` needed when using Ollama.

### Recommended models

| Model | Size | Quality | Speed |
|---|---|---|---|
| `mistral` | ~4GB | Best | Medium |
| `llama3.2` | ~2GB | Good | Fast |
| `llama3.1:8b` | ~5GB | Best | Slow |

> **Note:** Ollama responses take 10–60 seconds per summary depending on your hardware, compared to ~3 seconds with Claude. Urgency classification may also be less accurate with smaller models.

### Ollama as a Docker container

If you prefer to run Ollama in Docker alongside release-notifier:

```yaml
services:
  ollama:
    image: ollama/ollama
    container_name: ollama
    volumes:
      - ./ollama:/root/.ollama
    # Pull the model after first start:
    # docker exec ollama ollama pull mistral

  release-notifier:
    image: 57faruk57/release-notifier:latest
    environment:
      - AI_PROVIDER=ollama
      - OLLAMA_URL=http://ollama:11434
      - OLLAMA_MODEL=mistral
      - NTFY_URL=https://ntfy.sh/your-topic
```

## API

| Endpoint | Description |
|---|---|
| `GET /api/health` | Status, last/next check time, AI provider, docker socket status |
| `GET /api/updates` | Last 10 detected updates as JSON |
| `GET /api/latest` | Most recent update (for Homepage customapi widget) |
| `POST /api/check` | Trigger immediate check (requires X-API-Key header if API_KEY is set) |
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

## Triggering a manual check

```bash
# Without API key
curl -X POST http://localhost:8080/api/check

# With API key set
curl -X POST http://localhost:8080/api/check -H "X-API-Key: your_key"
```

## License

MIT — see [LICENSE](LICENSE) for details.

## Credits

Built with the assistance of [Claude](https://claude.ai) by Anthropic.
