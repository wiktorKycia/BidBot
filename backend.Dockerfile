FROM python:3.14-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# kopiujemy tylko pliki z kodem
COPY api.py ./
COPY etl/ ./etl/

# budowanie lokalnej paczki etl (rozwiązuje problem z importami)
RUN uv pip install -e .
# dodawanie zbudowanej paczki do znanych przez uv
RUN uv sync

CMD ["uv", "run", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]