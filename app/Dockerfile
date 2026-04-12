# ---- build stage ----
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---- runtime stage ----
FROM python:3.12-slim

LABEL org.opencontainers.image.title="release-notifier"
LABEL org.opencontainers.image.description="Watches GitHub releases for your Docker stack, summarises changelogs with Claude AI, and pushes notifications via ntfy."
LABEL org.opencontainers.image.url="https://github.com/Farukk57/release-notifier"
LABEL org.opencontainers.image.source="https://github.com/Farukk57/release-notifier"
LABEL org.opencontainers.image.licenses="MIT"

# Non-root user for security
RUN groupadd -r notifier && useradd -r -g notifier -s /sbin/nologin notifier

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application
COPY main.py .

# Data directories — owner notifier so the app can write state files
# These are expected to be mounted as volumes in production
RUN mkdir -p /config /data && chown notifier:notifier /config /data

USER notifier

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import httpx; httpx.get('http://localhost:8080/api/health', timeout=4).raise_for_status()"

CMD ["python3", "main.py"]
