FROM ghcr.io/astral-sh/uv:0.13.8 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra remote --no-install-project
COPY adaptive_rag ./adaptive_rag
COPY README.md ./
RUN uv sync --frozen --no-dev --extra remote && chmod -R a+rX /app/adaptive_rag && useradd --uid 10001 --create-home rag && mkdir /data && chown rag:rag /data
ENV PATH="/app/.venv/bin:$PATH" RAG_DATA_DIR=/data
USER rag
EXPOSE 8000
CMD ["uvicorn", "adaptive_rag.server:create", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
