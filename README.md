# Front Office Copilot

Multi-agent football front-office system. See [`Front Office Copilot Spec.md`](./Front%20Office%20Copilot%20Spec.md) for the full build spec and [`docs/architecture.md`](./docs/architecture.md) for the current architecture snapshot.

## Layout

```
/api          FastAPI app + LangGraph graphs, agents, tools
/ingestion    scrapers + dataset importers, one module per source
/infra        Dockerfiles, compose, k8s manifests, terraform
/docs         architecture.md, decisions.md, data-sources.md
/sql          migrations, views (numbered, idempotent)
/tests        pytest
```

## Local dev

```bash
uv sync --all-groups        # install deps (dev group included)
uv run ruff check .         # lint
uv run ruff format --check .
uv run mypy                 # typecheck
uv run pytest               # tests
```

Python 3.12 is pinned via `.python-version` — `uv` will fetch it automatically.

## Run the API

Directly with uvicorn:

```bash
uv run uvicorn api.main:app --reload
curl http://localhost:8000/health   # -> {"status":"ok"}
```

Or via Docker Compose (image + healthcheck):

```bash
docker compose -f infra/docker-compose.yml up --build
```

Logs are emitted as one JSON object per line (structlog).
