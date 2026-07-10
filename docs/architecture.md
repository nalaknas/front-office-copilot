# Architecture v1

Snapshot as of Phase 0. Will be updated at the end of every phase per the DoD in the spec.

## System diagram

The version below is annotated to distinguish "built today" (solid edges) from "arrives later" (dashed edges to Langfuse and OTel). For a cleaner "system at a glance" cut that ignores the Phase 0 vs. deferred distinction, see [`Front Office Architecture.mermaid`](./Front%20Office%20Architecture.mermaid).

```mermaid
flowchart LR
    user(["User"]) --> fe["Next.js frontend<br/>(Vercel)"]

    subgraph runtime["Cloud Run container"]
        api["FastAPI"]
        graph["LangGraph supervisor<br/>+ Scout / Valuation / Compliance"]
        api --> graph
    end

    fe -->|"HTTPS + API key"| api

    subgraph spine["Supabase (Postgres)"]
        rel[("Relational tables<br/>players, contracts,<br/>transfers, finances,<br/>watchlist, freshness")]
        vec[("pgvector<br/>regulation_chunks")]
        ckpt[("LangGraph<br/>checkpointer")]
    end

    graph <--> rel
    graph <--> vec
    graph <--> ckpt

    subgraph external["External data sources"]
        fbref["FBref (scrape)"]
        understat["Understat (scrape)"]
        tm["Transfermarkt<br/>via Kaggle"]
        capology["Capology (scrape)"]
        ch["UK Companies House<br/>REST API"]
    end

    subgraph ingest["GitHub Actions<br/>cron ingestion"]
        workers["Ingestion workers"]
    end

    workers --> fbref
    workers --> understat
    workers --> tm
    workers --> capology
    workers --> ch
    workers --> rel

    graph -.-> anthropic["Anthropic API<br/>(claude-sonnet-4-6)"]

    api -. traces .-> langfuse["Langfuse<br/>(LLM traces + evals)"]
    graph -. traces .-> langfuse
    api -. RED metrics .-> otel["OpenTelemetry SDK<br/>→ Grafana Cloud"]
    workers -. RED metrics .-> otel

    classDef deferred stroke-dasharray: 4 4;
    class langfuse,otel deferred;
```

## What ships today (Phase 0)

- **`/api`** — FastAPI process with `/health`. Structured JSON logging via structlog; uvicorn access/error logs are captured through the same handler so every line is one JSON record. See [`api/main.py`](../api/main.py), [`api/logging.py`](../api/logging.py).
- **Container image** — multi-stage Dockerfile: `python:3.12-slim-bookworm` in both stages; uv is present only in the builder. Runtime drops to non-root `appuser` and declares a `HEALTHCHECK` that hits `/health`. Currently 266 MB (target <300 MB). See [`infra/docker/Dockerfile.api`](../infra/docker/Dockerfile.api).
- **Local orchestration** — `infra/docker-compose.yml` runs the API service; healthcheck is inherited from the image.
- **CI** — GitHub Actions runs `ruff check`, `ruff format --check`, `mypy` (strict), and `pytest` on every push and PR via uv. See [`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

## What is scaffolded but empty

- **`/ingestion`** — one module per data source, will land in Phase 1 (FOC-7 through FOC-11).
- **`/sql`** — numbered idempotent migrations, first migration in FOC-6.
- **`/docs`** — this file. Decisions log + data-sources doc arrive as they become useful.
- **Frontend** — deferred until Phase 7 (FOC-28/29). Not scaffolded yet.

## Deferred pieces (dotted lines in the diagram)

| Piece | Ticket | Notes |
|---|---|---|
| Cloud Run deploy from GitHub Actions | FOC-4 | Deferred at user request; will come back after Phase 1. Spec §10 DoD says main should always be live from Phase 0 — accepting the gap intentionally. |
| Langfuse LLM tracing | FOC-25 | Wire via callback in the LLM wrapper (FOC-15) so instrumentation lands in one place. |
| OpenTelemetry / Grafana Cloud | FOC-27 | Instruments FastAPI + ingestion workers. |
| Human-in-the-loop approval | FOC-23 | LangGraph checkpointer + `/runs/{id}/approve` interrupt. |
| Signals worker (sentiment) | FOC-34 | Table stays out of Phase 1 by spec. `watchlist` table is built now as the future input surface. |

## Design principles worth stating once

- **Single Supabase spine.** Relational data, pgvector embeddings, and LangGraph checkpoints all live in the same Postgres instance. No second vector DB.
- **RAG is surgical.** Vector search is used only for regulation text. Structured questions (stats, wages, finances) go through SQL tools, not retrieval.
- **All LLM calls flow through one wrapper module** (FOC-15) so retry / timeout / typed responses / observability instrumentation happen in one place.
- **Pydantic at every boundary.** No untyped dicts crossing agent, tool, or API edges.
- **Idempotent everything.** Migrations, upserts, and ingestion runs must all be safely re-runnable. Every ingestion writes a row to `data_freshness`.
