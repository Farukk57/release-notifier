import html
import os
import re
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import docker
import httpx
import yaml
from anthropic import Anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request, abort

# ---- logging -----------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---- paths -------------------------------------------------------------------
CONFIG_FILE   = Path("/config/containers.yml")
STATE_FILE    = Path("/data/state.json")
UPDATES_FILE  = Path("/data/updates.json")
NOTIFIED_FILE = Path("/data/notified.json")

# ---- flask -------------------------------------------------------------------
app = Flask(__name__)

# ---- module-level singletons and state ---------------------------------------
_scheduler_ref   = None
_last_check_at   = None
_last_check_ok   = None
_check_lock      = threading.Lock()   # prevents concurrent check_releases runs
_anthropic_client = None              # created once on first check
_docker_client   = None               # cached Docker client

# ---- performance: pre-compiled regex -----------------------------------------
_RE_BOLD = re.compile(r"\*\*(.+?)\*\*")

# ---- performance: simple in-memory cache for file reads ----------------------
_file_cache: dict = {}   # {path: (mtime, data)}

def _cached_read(path: Path, loader):
    """Return parsed file contents, re-reading only when the file has changed."""
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return loader(None)
    key = str(path)
    if key in _file_cache and _file_cache[key][0] == mtime:
        return _file_cache[key][1]
    data = loader(path)
    _file_cache[key] = (mtime, data)
    return data

def _invalidate_cache(path: Path):
    _file_cache.pop(str(path), None)

# ---- OCI labels for version detection ----------------------------------------
VERSION_LABELS = [
    "org.opencontainers.image.version",
    "build_version",
    "version",
]

# ---- performance: registry token cache (per registry+repo, TTL 8 min) --------
_token_cache: dict = {}   # {(registry, repo): (expires_at, token)}
_TOKEN_TTL = 480          # seconds


# ---- config / state helpers --------------------------------------------------

def load_config():
    def _read(p):
        if p is None:
            raise FileNotFoundError(f"{CONFIG_FILE} not found")
        with open(p) as f:
            cfg = yaml.safe_load(f)
        # Basic schema validation
        if not isinstance(cfg, dict):
            raise ValueError("containers.yml must be a YAML mapping")
        for c in cfg.get("containers", []):
            if "name" not in c:
                raise ValueError(f"Container entry missing 'name': {c}")
            if "github" not in c:
                raise ValueError(f"Container '{c.get('name')}' missing 'github' field")
        return cfg
    return _cached_read(CONFIG_FILE, _read)

def load_state():
    def _read(p):
        if p is None:
            return {}
        return json.loads(p.read_text())
    return _cached_read(STATE_FILE, _read)

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))
    _invalidate_cache(STATE_FILE)

def load_updates():
    def _read(p):
        if p is None:
            return []
        return json.loads(p.read_text())
    return _cached_read(UPDATES_FILE, _read)

def save_updates(updates):
    UPDATES_FILE.write_text(json.dumps(updates, indent=2))
    _invalidate_cache(UPDATES_FILE)

def load_notified():
    def _read(p):
        if p is None:
            return {}
        return json.loads(p.read_text())
    return _cached_read(NOTIFIED_FILE, _read)

def save_notified(notified):
    NOTIFIED_FILE.write_text(json.dumps(notified, indent=2))
    _invalidate_cache(NOTIFIED_FILE)

def already_notified(notified, repo, tag):
    return tag in notified.get(repo, [])

def mark_notified(notified, repo, tag):
    notified.setdefault(repo, [])
    if tag not in notified[repo]:
        notified[repo].append(tag)
    notified[repo] = notified[repo][-5:]


# ---- security: URL validation ------------------------------------------------

def safe_url(url: str) -> str:
    """Return url if it starts with https://, otherwise a safe fallback."""
    if url and url.startswith("https://"):
        return url
    return "https://github.com"


# ---- security: API key auth for mutating endpoints ---------------------------

def _get_api_key() -> str | None:
    """Return the configured API key from env, or None if not set."""
    return os.environ.get("API_KEY") or None

def require_api_key():
    """
    Abort with 401 if API_KEY env var is set and the request does not
    supply it via X-API-Key header or ?api_key= query param.
    """
    key = _get_api_key()
    if key is None:
        return  # auth disabled -- internal/trusted network use only
    provided = (
        request.headers.get("X-API-Key") or
        request.args.get("api_key") or
        ""
    )
    if not secrets.compare_digest(provided, key):
        log.warning(f"Unauthorized /api/check attempt from {request.remote_addr}")
        abort(401)


# ---- retry helper ------------------------------------------------------------

def with_retry(fn, retries=3, backoff=5):
    wait = backoff
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == retries:
                log.warning(f"All {retries} attempts failed: {e}")
                return None
            log.warning(f"Attempt {attempt}/{retries} failed: {e} -- retrying in {wait}s")
            time.sleep(wait)
            wait *= 2
    return None


# ---- pre-release filtering ---------------------------------------------------

def is_prerelease(release):
    if release.get("prerelease", False):
        return True
    if release.get("draft", False):
        return True
    tag = release.get("tag_name", "").lower()
    if re.search(r"(alpha|beta|rc\d*|nightly|dev|preview|unstable|snapshot)", tag):
        return True
    return False


def get_latest_release(github_repo, github_token=None, stable_only=True):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    def _fetch():
        if stable_only:
            url = f"https://api.github.com/repos/{github_repo}/releases/latest"
            r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
            r.raise_for_status()
            release = r.json()
            if is_prerelease(release):
                log.info(f"{github_repo}: /latest returned a pre-release -- skipping")
                return None
            return release
        else:
            url = f"https://api.github.com/repos/{github_repo}/releases"
            r = httpx.get(url, headers=headers, params={"per_page": 5}, timeout=15, follow_redirects=True)
            r.raise_for_status()
            for rel in r.json():
                if not rel.get("draft", False):
                    return rel
            return None

    return with_retry(_fetch, retries=3, backoff=5)


# ---- registry digest tracking ------------------------------------------------

def parse_image_ref(image_ref):
    if image_ref.startswith("lscr.io/"):
        return "ghcr.io", image_ref[len("lscr.io/"):]
    parts = image_ref.split("/", 1)
    if len(parts) == 2 and ("." in parts[0] or ":" in parts[0]):
        registry, repo = parts[0], parts[1]
        if registry == "docker.io" and "/" not in repo:
            repo = f"library/{repo}"
        return registry, repo
    if "/" not in image_ref:
        return "docker.io", f"library/{image_ref}"
    return "docker.io", image_ref


def get_registry_token(registry, repo):
    """Fetch anonymous pull token, using an 8-minute in-memory cache."""
    cache_key = (registry, repo)
    cached = _token_cache.get(cache_key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    try:
        if registry == "ghcr.io":
            r = httpx.get(
                "https://ghcr.io/token",
                params={"service": "ghcr.io", "scope": f"repository:{repo}:pull"},
                timeout=10,
            )
            r.raise_for_status()
            token = r.json().get("token")
        elif registry == "docker.io":
            r = httpx.get(
                "https://auth.docker.io/token",
                params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"},
                timeout=10,
            )
            r.raise_for_status()
            token = r.json().get("token")
        else:
            return None

        if token:
            _token_cache[cache_key] = (time.monotonic() + _TOKEN_TTL, token)
        return token

    except Exception as e:
        log.debug(f"Token fetch failed for {registry}/{repo}: {e}")
    return None


def get_remote_digest(image_ref, tag):
    registry, repo = parse_image_ref(image_ref)
    token = get_registry_token(registry, repo)
    if not token:
        return None

    accept = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ])
    headers = {"Authorization": f"Bearer {token}", "Accept": accept}
    candidates = [tag, tag[1:]] if tag.startswith("v") else [tag]
    base_url = (
        f"https://ghcr.io/v2/{repo}/manifests"
        if registry == "ghcr.io"
        else f"https://registry-1.docker.io/v2/{repo}/manifests"
    )

    for candidate in candidates:
        try:
            r = httpx.head(f"{base_url}/{candidate}", headers=headers, timeout=10, follow_redirects=True)
            if r.status_code == 200:
                digest = r.headers.get("Docker-Content-Digest")
                if digest:
                    return digest
        except Exception as e:
            log.debug(f"Registry HEAD failed for {image_ref}:{candidate}: {e}")
    return None


def get_local_info(running_containers, container_name, image_ref, docker_container_name=None):
    """
    Find the running container from a pre-fetched list and return (digest, running_version).
    Accepts running_containers to avoid repeated containers.list() calls.
    """
    if running_containers is None:
        return None, None

    registry, repo = parse_image_ref(image_ref)
    search_names = [container_name.lower()]
    if docker_container_name:
        search_names.append(docker_container_name.lower())

    try:
        for c in running_containers:
            names      = [n.lstrip("/").lower() for n in c.attrs.get("Names", [])]
            image_tags = c.image.tags

            name_match  = any(sn in n for sn in search_names for n in names)
            image_match = any(sn in t.lower() for sn in search_names for t in image_tags)

            if not (name_match or image_match):
                continue

            labels = c.image.attrs.get("Config", {}).get("Labels") or {}
            running_version = None
            for label_key in VERSION_LABELS:
                val = labels.get(label_key)
                if val:
                    running_version = val.split()[0].strip()
                    log.info(f"{container_name}: running version from label '{label_key}' = {running_version}")
                    break

            repo_digests = c.image.attrs.get("RepoDigests", [])
            digest = None
            for rd in repo_digests:
                if f"/{repo}@" in rd or rd.startswith(f"{registry}/{repo}@"):
                    digest = rd.split("@", 1)[1] if "@" in rd else None
                    if digest:
                        break
            if not digest:
                for rd in repo_digests:
                    if "@sha256:" in rd:
                        digest = rd.split("@", 1)[1]
                        break

            return digest, running_version

    except Exception as e:
        log.debug(f"Local info lookup failed for {container_name}: {e}")
    return None, None


def check_pull_status(running_containers, container_name, image_ref, new_tag, docker_container_name=None):
    if not image_ref:
        return "unknown", None

    remote = get_remote_digest(image_ref, new_tag)
    if not remote:
        log.info(f"{container_name}: could not fetch remote digest for {new_tag}")
        _, running_version = get_local_info(running_containers, container_name, image_ref, docker_container_name)
        return "unknown", running_version

    local_digest, running_version = get_local_info(
        running_containers, container_name, image_ref, docker_container_name
    )

    if not local_digest:
        log.info(f"{container_name}: could not read local digest")
        return "unknown", running_version

    if local_digest == remote:
        log.info(f"{container_name}: digest match -- already up to date")
        return "up_to_date", running_version
    else:
        log.info(f"{container_name}: digest mismatch -- pull required")
        return "pull_required", running_version


# ---- summarize ---------------------------------------------------------------

def summarize_release(client, container_name, version, release_body,
                      pull_status="unknown", running_version=None):
    if not release_body or not release_body.strip():
        return "No release notes provided.", "default"

    truncated   = release_body[:6000]
    version_ctx = f"Currently running: {running_version}\n" if (running_version and running_version != version) else ""
    pull_ctx    = {
        "up_to_date":    "Note: the running container image already matches this release digest -- no pull needed.",
        "pull_required": "Note: the running container image does NOT yet match this release -- a pull is required.",
        "unknown":       "",
    }.get(pull_status, "")

    prompt = f"""You are summarizing a software release for a self-hosted homelab enthusiast.

Container: {container_name}
New version: {version}
{version_ctx}{pull_ctx}

Release notes:
{truncated}

First, output a single line with the urgency level for this release, using EXACTLY one of:
URGENCY: urgent
URGENCY: high
URGENCY: default

Use these definitions strictly:
- urgent: the release patches a real security vulnerability that affects this container directly (CVE, authentication bypass, RCE, privilege escalation, data exposure). Not for transitive dependency patches or Windows-only issues on a Linux server.
- high: the release contains breaking changes, required migration steps, or a deprecation that requires user action.
- default: everything else including feature releases, bug fixes, and maintenance updates.

Then output the summary using this exact structure:

One sentence describing what kind of release this is (e.g. security patch, feature release, hotfix).

**What's new**
2-3 bullet points covering the most important new features or changes. Be specific.

**Fixes**
2-3 bullet points for notable bug fixes or security patches. If a security fix, mention the CVE or vulnerability type.

**Action required**
One line: either "No breaking changes or migration steps required." or a brief description of what the user needs to do.

Keep each bullet point to one concise sentence. Do not add any intro or outro text beyond the URGENCY line and the summary."""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=450,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        lines = raw.split("\n", 1)
        urgency = "default"
        summary = raw
        if lines[0].strip().startswith("URGENCY:"):
            urgency_raw = lines[0].replace("URGENCY:", "").strip().lower()
            if urgency_raw in ("urgent", "high", "default"):
                urgency = urgency_raw
            summary = lines[1].strip() if len(lines) > 1 else ""
            log.info(f"Urgency classified by Claude: {urgency}")
        return summary, urgency
    except Exception as e:
        log.error(f"Anthropic API error: {e}")
        return f"Release {version} is available. (AI summary unavailable)", "default"


# ---- ntfy --------------------------------------------------------------------

def send_ntfy(ntfy_url, container_name, new_version, summary, release_url,
              urgency="default", pull_status="unknown",
              running_version=None, ntfy_token=None):

    release_url = safe_url(release_url)

    if running_version and running_version != new_version and pull_status == "pull_required":
        title = f"{container_name}: {running_version} -> {new_version}"
    elif pull_status == "up_to_date":
        title = f"{container_name} {new_version} - already up to date"
    elif pull_status == "pull_required":
        title = f"{container_name} {new_version} - pull required"
    else:
        title = f"{container_name} {new_version}"

    priority_map = {
        "urgent":  ("urgent",  "package,warning,rotating_light"),
        "high":    ("high",    "package,tada,arrow_up"),
        "default": ("default", "package,tada,rocket"),
    }
    priority, tags = priority_map.get(urgency, priority_map["default"])

    headers = {
        "Title":        title,
        "Tags":         tags,
        "Priority":     priority,
        "Actions":      f"view, Release notes, {release_url}",
        "Content-Type": "text/plain; charset=utf-8",
        "Markdown":     "yes",
    }
    if ntfy_token:
        headers["Authorization"] = f"Bearer {ntfy_token}"

    try:
        r = httpx.post(ntfy_url, content=summary.encode(), headers=headers, timeout=10)
        r.raise_for_status()
        log.info(f"Sent ntfy notification for {container_name} {new_version} [priority={priority}, pull={pull_status}, running={running_version}]")
    except Exception as e:
        log.error(f"ntfy error for {container_name}: {e}")


# ---- Docker client (cached singleton) ----------------------------------------

def get_docker_client():
    global _docker_client
    if _docker_client is not None:
        try:
            _docker_client.ping()
            return _docker_client
        except Exception:
            _docker_client = None
    try:
        client = docker.from_env()
        client.ping()
        _docker_client = client
        return client
    except Exception:
        return None


# ---- main check loop ---------------------------------------------------------

def check_releases():
    global _last_check_at, _last_check_ok, _anthropic_client

    # Prevent concurrent runs
    if not _check_lock.acquire(blocking=False):
        log.warning("Check already in progress -- skipping concurrent trigger")
        return

    try:
        log.info("Checking for new releases...")
        cfg      = load_config()
        settings = cfg.get("settings", {})

        ntfy_url     = os.environ.get("NTFY_URL",         settings.get("ntfy_url", ""))
        ntfy_token   = os.environ.get("NTFY_TOKEN",        settings.get("ntfy_token"))
        github_token = os.environ.get("GITHUB_TOKEN",      settings.get("github_token"))
        api_key      = os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            log.error("ANTHROPIC_API_KEY not set -- cannot summarize releases.")
            _last_check_at = datetime.now(timezone.utc).isoformat()
            _last_check_ok = False
            return
        if not ntfy_url:
            log.warning("NTFY_URL not set -- notifications will be skipped.")

        # Reuse cached Anthropic client
        if _anthropic_client is None:
            _anthropic_client = Anthropic(api_key=api_key)

        docker_client = get_docker_client()
        if docker_client:
            log.info("Docker socket connected -- digest + version tracking enabled.")
        else:
            log.info("Docker socket not available -- digest tracking disabled.")

        # Fetch all running containers ONCE before the loop
        running_containers = None
        if docker_client:
            try:
                running_containers = docker_client.containers.list()
            except Exception as e:
                log.warning(f"Could not list Docker containers: {e}")

        state    = load_state()
        updates  = load_updates()
        notified = load_notified()

        for container in cfg.get("containers", []):
            name                  = container["name"]
            repo                  = container["github"]
            image_ref             = container.get("image")
            docker_container_name = container.get("container")
            stable_only           = container.get("stable_only", settings.get("stable_only", True))

            release = get_latest_release(repo, github_token, stable_only=stable_only)
            if not release:
                continue

            tag = release.get("tag_name", "")
            if not tag:
                continue

            known_tag = state.get(repo)
            if known_tag == tag:
                log.info(f"{name}: already at {tag}")
                continue

            log.info(f"New release: {name} {tag} (was {known_tag or 'unknown'})")

            body        = release.get("body", "")
            release_url = safe_url(release.get("html_url", f"https://github.com/{repo}/releases"))
            published   = release.get("published_at", datetime.now(timezone.utc).isoformat())

            pull_status, running_version = check_pull_status(
                running_containers, name, image_ref, tag, docker_container_name
            )
            log.info(f"{name}: pull_status={pull_status}, running_version={running_version}")

            summary, urgency = summarize_release(
                _anthropic_client, name, tag, body, pull_status, running_version
            )

            if ntfy_url:
                if already_notified(notified, repo, tag):
                    log.info(f"{name} {tag}: notification already sent -- skipping")
                else:
                    send_ntfy(
                        ntfy_url, name, tag, summary, release_url,
                        urgency=urgency,
                        pull_status=pull_status,
                        running_version=running_version,
                        ntfy_token=ntfy_token,
                    )
                    mark_notified(notified, repo, tag)
                    save_notified(notified)

            record = {
                "container":       name,
                "github":          repo,
                "version":         tag,
                "running_version": running_version,
                "pull_status":     pull_status,
                "urgency":         urgency,
                "published":       published,
                "summary":         summary,
                "release_url":     release_url,
                "detected_at":     datetime.now(timezone.utc).isoformat(),
            }
            updates.insert(0, record)
            updates = updates[:50]
            save_updates(updates)

            state[repo] = tag
            save_state(state)

            time.sleep(1)

        _last_check_at = datetime.now(timezone.utc).isoformat()
        _last_check_ok = True
        log.info("Check complete.")

    except Exception as e:
        log.error(f"Check cycle failed: {e}", exc_info=True)
        _last_check_at = datetime.now(timezone.utc).isoformat()
        _last_check_ok = False
    finally:
        _check_lock.release()


# ---- Flask API ---------------------------------------------------------------

@app.route("/api/updates")
def api_updates():
    updates = load_updates()
    return jsonify({"updates": updates[:10], "count": len(updates)})


@app.route("/api/latest")
def api_latest():
    updates = load_updates()
    if not updates:
        return jsonify({"container": "--", "version": "--", "summary": "No updates yet."})
    u = updates[0]
    return jsonify({
        "container":       u["container"],
        "version":         u["version"],
        "running_version": u.get("running_version"),
        "pull_status":     u.get("pull_status", "unknown"),
        "urgency":         u.get("urgency", "default"),
        "summary":         u["summary"],
        "url":             u["release_url"],
        "detected":        u["detected_at"][:10],
    })


@app.route("/api/health")
def api_health():
    # Use cached Docker client -- don't ping on every request
    docker_ok = _docker_client is not None
    if not docker_ok:
        # Try to connect if not yet established
        docker_ok = get_docker_client() is not None

    cfg   = load_config()
    state = load_state()
    containers_with_image = sum(1 for c in cfg.get("containers", []) if c.get("image"))

    next_run = None
    if _scheduler_ref is not None:
        jobs = _scheduler_ref.get_jobs()
        if jobs:
            nr = jobs[0].next_run_time
            next_run = nr.isoformat() if nr else None

    return jsonify({
        "status":           "ok",
        "docker_socket":    docker_ok,
        "watched":          len(cfg.get("containers", [])),
        "digest_tracking":  containers_with_image,
        "known_versions":   len(state),
        "last_check_at":    _last_check_at,
        "last_check_ok":    _last_check_ok,
        "next_check_at":    next_run,
        "check_running":    _check_lock.locked(),
        "auth_enabled":     _get_api_key() is not None,
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    require_api_key()
    if _check_lock.locked():
        return jsonify({"status": "already running"}), 409
    threading.Thread(target=check_releases, daemon=True).start()
    return jsonify({"status": "check triggered"})


@app.route("/")
def index():
    updates = load_updates()
    cfg = load_config()
    return jsonify({
        "service": "release-notifier",
        "watched": [c["name"] for c in cfg.get("containers", [])],
        "updates": updates[:5],
    })


@app.route("/dashboard")
def dashboard():
    updates = load_updates()

    def render_markdown(text):
        """Convert markdown to HTML with proper escaping."""
        lines = text.split("\n")
        html_lines = []
        in_ul = False
        for line in lines:
            stripped = line.strip()
            # Escape HTML entities FIRST, then apply markdown formatting
            escaped = html.escape(stripped)
            if stripped.startswith("- ") or stripped.startswith("* "):
                if not in_ul:
                    html_lines.append("<ul>")
                    in_ul = True
                content = _RE_BOLD.sub(r"<strong>\1</strong>", html.escape(stripped[2:]))
                html_lines.append(f"<li>{content}</li>")
            else:
                if in_ul:
                    html_lines.append("</ul>")
                    in_ul = False
                if stripped == "":
                    html_lines.append("<br>")
                else:
                    content = _RE_BOLD.sub(r"<strong>\1</strong>", escaped)
                    html_lines.append(f"<p>{content}</p>")
        if in_ul:
            html_lines.append("</ul>")
        return "\n".join(html_lines)

    urgency_styles = {
        "urgent":  ("border-left: 3px solid #f85149;", "&#x1F6A8;"),
        "high":    ("border-left: 3px solid #d29922;", "&#x26A0;&#xFE0F;"),
        "default": ("border-left: 3px solid rgba(255,255,255,0.08);", ""),
    }
    pull_badges = {
        "up_to_date":    '<span class="badge badge-ok">&#10003; up to date</span>',
        "pull_required": '<span class="badge badge-pull">&#8595; pull required</span>',
        "unknown":       "",
    }

    cards = ""
    for u in updates[:10]:
        date            = html.escape(u.get("detected_at", "")[:10])
        urgency         = u.get("urgency", "default")
        pull_status     = u.get("pull_status", "unknown")
        raw_version     = u.get("version", "")
        running_version = u.get("running_version")
        summary_html    = render_markdown(u.get("summary", ""))
        border_style, urgency_icon = urgency_styles.get(urgency, urgency_styles["default"])
        pull_badge      = pull_badges.get(pull_status, "")

        # Escape all user-controlled strings before injecting into HTML
        safe_container = html.escape(u.get("container", ""))
        safe_version   = html.escape(raw_version)
        safe_running   = html.escape(running_version) if running_version else None
        safe_url_val   = html.escape(safe_url(u.get("release_url", "")))

        if safe_running and safe_running != safe_version and pull_status == "pull_required":
            version_display = f"{safe_running} &#8594; {safe_version}"
        else:
            version_display = safe_version

        cards += f"""
        <div class="card" style="{border_style}">
          <div class="card-header">
            <div class="card-title">
              <span class="urgency-icon">{urgency_icon}</span>
              <span class="container-name">{safe_container}</span>
              <span class="version">{version_display}</span>
              {pull_badge}
            </div>
            <div class="card-meta">
              <span class="date">{date}</span>
              <a href="{safe_url_val}" target="_blank" rel="noopener noreferrer" class="release-link">Release notes &#8599;</a>
            </div>
          </div>
          <div class="card-body">{summary_html}</div>
        </div>"""

    if not cards:
        cards = '<div class="empty">No updates detected yet.</div>'

    html_out = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'none';">
  <title>Release Notifier</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; font-size: 13px; background: transparent; color: #c9d1d9; padding: 8px; }}
    .card {{ background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; margin-bottom: 8px; overflow: hidden; }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; padding: 8px 12px; background: rgba(255,255,255,0.05); border-bottom: 1px solid rgba(255,255,255,0.06); gap: 8px; flex-wrap: wrap; }}
    .card-title {{ display: flex; align-items: center; gap: 6px; }}
    .urgency-icon {{ font-size: 12px; }}
    .container-name {{ font-weight: 600; color: #e6edf3; font-size: 13px; }}
    .version {{ background: rgba(56,139,253,0.15); color: #58a6ff; border: 1px solid rgba(56,139,253,0.3); border-radius: 4px; padding: 1px 7px; font-size: 11px; font-family: ui-monospace, monospace; }}
    .badge {{ border-radius: 4px; padding: 1px 7px; font-size: 11px; }}
    .badge-ok {{ background: rgba(63,185,80,0.15); color: #3fb950; border: 1px solid rgba(63,185,80,0.3); }}
    .badge-pull {{ background: rgba(210,153,34,0.15); color: #d29922; border: 1px solid rgba(210,153,34,0.3); }}
    .card-meta {{ display: flex; align-items: center; gap: 10px; }}
    .date {{ color: #6e7681; font-size: 11px; }}
    .release-link {{ color: #58a6ff; text-decoration: none; font-size: 11px; }}
    .release-link:hover {{ text-decoration: underline; }}
    .card-body {{ padding: 10px 12px; line-height: 1.55; color: #b1bac4; }}
    .card-body p {{ margin-bottom: 4px; }}
    .card-body p:last-child {{ margin-bottom: 0; }}
    .card-body strong {{ color: #e6edf3; }}
    .card-body ul {{ padding-left: 16px; margin: 3px 0; }}
    .card-body li {{ margin-bottom: 2px; }}
    .empty {{ text-align: center; color: #6e7681; padding: 24px; }}
  </style>
</head>
<body>{cards}</body>
</html>"""
    return html_out


# ---- entry point -------------------------------------------------------------

if __name__ == "__main__":
    from waitress import serve

    cfg      = load_config()
    interval = cfg.get("settings", {}).get("check_interval_hours", 6)
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_releases, "interval", hours=interval, next_run_time=datetime.now())
    scheduler.start()
    _scheduler_ref = scheduler

    api_key_set = _get_api_key() is not None
    log.info(f"Scheduler started -- checking every {interval}h")
    log.info(f"API key auth: {'enabled' if api_key_set else 'disabled (set API_KEY env var to enable)'}")
    log.info("Starting production server on :8080")
    serve(app, host="0.0.0.0", port=8080, threads=4)
