# Multi-stage build for a lean, secure production image
FROM python:3.11-slim AS builder

WORKDIR /app

# Install system build dependencies required for compiling C-extensions (psycopg, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install modern wheel building tools
RUN pip install --no-cache-dir --upgrade pip build

# Copy package config first to leverage Docker layer caching
COPY pyproject.toml .

# Install dependencies into a isolated wheels directory
RUN pip wheel --no-cache-dir --wheel-dir /app/wheels .

# --- Runtime Stage ---
FROM python:3.11-slim AS runner

WORKDIR /app

# Install essential runtime tools (curl for healthchecks, libpq for postgres)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Copy built wheels from builder and install them
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir /wheels/* \
    && rm -rf /wheels

# Copy application source code, migrations, and alembic configs
COPY app/ ./app
COPY static/ ./static
COPY migrations/ ./migrations
COPY alembic.ini .
COPY pyproject.toml .

# Ensure cache directory exists and set ownership
RUN mkdir -p /app/.cache && chown -R appuser:appuser /app

# Switch to non-root execution
USER appuser

EXPOSE 8000

# Health check to ensure Uvicorn is serving requests
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/docs || exit 1

# Production server execution with worker tuning
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]