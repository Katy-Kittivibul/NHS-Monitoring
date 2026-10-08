# Multi-stage: wheels are built in `builder` (which needs a compiler for any
# sdist-only dependency), then only the installed packages are copied into the
# runtime image. The runtime layer therefore carries no build toolchain.
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Requirements are copied alone first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim AS runtime

# PYTHONDONTWRITEBYTECODE keeps the bind-mounted source tree free of __pycache__
# owned by root; PYTHONUNBUFFERED makes the pipeline's progress prints stream.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY --from=builder /install /usr/local

# Source only. data/ is deliberately NOT copied: raw extracts and the DuckDB file
# are gitignored and can exceed the image size budget, so they are bind-mounted by
# docker-compose instead. This also keeps patient-adjacent data out of any image.
COPY etl/ ./etl/
COPY dq/ ./dq/
COPY cp/ ./cp/
COPY corr/ ./corr/
COPY tests/ ./tests/
COPY app/ ./app/
COPY pyproject.toml requirements.txt README.md ./

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data/processed /app/data/raw \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4).status==200 else 1)"

CMD ["streamlit", "run", "app/dashboard.py", \
     "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
