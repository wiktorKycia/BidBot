FROM python:3.14-slim

RUN apt-get update && apt-get install -y curl libreoffice-nogui antiword && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Pre-download spaCy NLP model during build to avoid runtime downloads
RUN uv pip install pip && uv run python -m spacy download en_core_web_sm

COPY api.py ./
COPY etl/ ./etl/

CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]