# vu-handbook-mcp — production image (Streamable HTTP + bearer auth + rate limit)
# Build from the REPO ROOT:  docker build -f deploy/Dockerfile -t vu-handbook-mcp .
# Differences from the root Dockerfile: non-root user, ships docs/lms (LMS tools),
# and runs deploy/serve.py (auth + rate limiting) instead of bare mcp_server.py.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_BIND=0.0.0.0 \
    MCP_PORT=8000

RUN pip install "mcp>=1.13,<2" \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin mcp

WORKDIR /app
COPY --chown=root:root mcp_server.py ./
COPY --chown=root:root data/handbook.db data/toc.json data/pages.jsonl ./data/
COPY --chown=root:root docs/lms/ ./docs/lms/
COPY --chown=root:root deploy/serve.py ./deploy/serve.py

USER mcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2).status==200 else 1)"

CMD ["python", "deploy/serve.py"]
