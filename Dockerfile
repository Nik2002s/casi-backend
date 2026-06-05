# ── CASI Backend — Production Dockerfile ──────────────────────────────────────
# Build:  docker build -t casi-backend .
# Run:    docker run -p 5001:5001 --env-file .env casi-backend

FROM python:3.12-slim AS base

# System deps (psycopg2-binary bundles libpq, so no extra apt needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies layer (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir gunicorn

# Application code
COPY . .

# Permanent file storage (mounted as volume in compose)
RUN mkdir -p storage/uploads

# Non-root user for security
RUN adduser --disabled-password --gecos '' casi \
    && chown -R casi:casi /app
USER casi

# Runtime
EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:5001/api/health || exit 1

# 4 workers, threads for I/O-bound DB/LLM calls
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--workers", "4", \
     "--threads", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
