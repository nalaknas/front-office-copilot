# Front Office Copilot — Build Spec

**Purpose of this doc:** Complete specification for an AI-powered football front-office system. Written to be dropped into Cursor as project context. Work ticket-by-ticket, in order, within each phase. Do not skip acceptance criteria.

---

## 1. Product Overview

A multi-agent system that acts as a football club's front office (Manchester United as the reference club). It:

1. **Evaluates the current squad** — performance vs. wage efficiency, position-adjusted, to surface renewal priorities and offload candidates
2. **Scouts external targets** — filtered through an investment thesis: best U23 talents, elite performance percentiles, wages that fit the club's wage structure
3. **Checks financial compliance** — PSR/FFP impact of any proposed transfer, grounded in actual regulation text via RAG, with book-value/amortization math
4. **Produces transfer recommendation memos** — a supervisor agent orchestrates scout, valuation, and compliance agents into a final, cited memo with human approval before finalization

**Investment thesis (encoded as scoring logic, not vibes):** target U23 players, top performance percentiles for their position, contract/wage demands that fit the club's wage bill construct, positive book-value dynamics.

**Deferred (do NOT build now):** news/sentiment signals worker. But DO build the `watchlist` table in Phase 1 — it's the future input surface.

---

## 2. Stack (locked — do not substitute)

| Layer | Choice |
|---|---|
| Language | Python 3.12, uv for dependency management |
| API | FastAPI + Pydantic v2 |
| Agents | LangGraph (supervisor pattern), Anthropic API (claude-sonnet-4-6 default; consider haiku for scout tool-call loops later) |
| Database | Supabase Postgres — single spine for relational data, pgvector, and LangGraph checkpoints |
| Vector search | pgvector extension (NOT a separate vector DB) |
| Frontend | Next.js on Vercel (thin: chat + dashboard). Reuse existing Supabase auth patterns |
| LLM observability | Langfuse (cloud free tier, or self-hosted via Docker later) |
| Service observability | OpenTelemetry SDK → Grafana Cloud free tier |
| Containers | Docker, multi-stage builds, non-root user |
| Hosting v1 | Cloud Run (scale-to-zero) via GitHub Container Registry |
| Hosting v2 (learning phase) | One managed K8s cluster (EKS or AKS) via Terraform, then torn down |
| CI/CD | GitHub Actions: lint (ruff), typecheck (mypy), test (pytest), build, deploy |
| Scheduling | GitHub Actions cron for ingestion workers |

**Repo layout (monorepo):**
```
/api          FastAPI app + LangGraph graphs, agents, tools
/ingestion    scrapers + dataset importers, one module per source
/infra        Dockerfiles, compose, k8s manifests (later), terraform (later)
/docs         architecture.md, decisions.md, data-sources.md
/frontend     Next.js app (or separate repo if preferred)
/sql          migrations, views (numbered, idempotent)
```

**Conventions for the agent/Cursor:**
- Every ticket = one branch = one PR. Keep PRs small.
- All SQL as numbered migration files in `/sql`, never ad-hoc.
- All external HTTP calls: typed client class per source, built-in rate limiting, response caching to disk in dev.
- All LLM calls go through one wrapper module (so Langfuse instrumentation lands in one place).
- Pydantic models for every API request/response and every agent output. No untyped dicts across boundaries.
- Update `/docs/architecture.md` at the end of every phase.

---

## 3. Data Sources & Cadence

| Domain | Source | Method | Cadence | Notes |
|---|---|---|---|---|
| Performance stats | FBref | Scrape, 1 req / 3–6 s, cache all pages | Weekly (post-matchweek) | Hard-throttled site. Incremental scrapes only. |
| xG/xA time series | Understat | Scrape (JSON embedded in pages) | Weekly | Easy to parse |
| Event data (later) | StatsBomb open data | `statsbombpy` | Static | Optional enrichment, Phase 9+ |
| Contracts, market values, transfer history | Kaggle dataset `davidcariboo/player-scores` (Transfermarkt-derived) | Dataset download via Kaggle API | Weekly | Do NOT scrape Transfermarkt directly (ToS) |
| Wages | Capology | Scrape, gentle | Monthly | Estimates — treat as such in docs |
| Club finances | UK Companies House API | Free REST API | Quarterly check (filings are annual) | Real filed accounts for every Prem club. Registration required for API key |
| Financial context docs | Deloitte Money League, UEFA/PL regulation PDFs | Manual download → Supabase Storage | Annual | Feeds RAG corpus |

Every ingestion run writes to a `data_freshness` table: `(source, last_run_at, status, rows_affected)`. The dashboard surfaces this.

---

## 4. Data Model (core tables — refine in migrations)

```
players            (id, name, dob, position, position_group, nationality, current_club_id, fbref_id, understat_id, tm_id)
clubs              (id, name, league, tm_id, companies_house_number)
player_stats       (player_id, season, matchweek_range, minutes, per-90 metric columns..., source)
contracts          (player_id, club_id, start_date, end_date, weekly_wage_est, wage_source)
transfers          (player_id, from_club_id, to_club_id, fee, date, contract_years)
club_finances      (club_id, fy_end, revenue, wage_bill, amortization, profit_loss, source_filing_url)
watchlist          (player_id, added_at, added_reason, thesis_fit_score)
regulation_chunks  (id, source_doc, article, section, content, embedding vector, metadata jsonb)
player_signals     (DEFERRED — do not create yet)
data_freshness     (source, last_run_at, status, rows_affected)
```

**Key SQL views (Phase 4):**
- `player_performance_index` — position-weighted per-90 composite → percentile within (league, position_group). Minutes floor: 900. Position weights defined in a config table, not hardcoded.
- `player_wage_context` — wage percentile within (league, position_group)
- `squad_efficiency` — joins the two + contract runway + age → efficiency_gap, quadrant label, renewal_priority_score, offload_priority_score
- `book_value` — per player: transfer fee amortized straight-line over contract years → remaining book value → potential PSR profit at a given sale price
- `thesis_targets` — external players passing thesis filters (U23, perf percentile ≥ 75, wage fit vs club wage structure, contract runway weighting)

---

## 5. Agent Architecture

**LangGraph supervisor pattern, single process, shared typed state:**

```python
class FrontOfficeState(TypedDict):
    messages: list
    target_player: dict | None
    scout_report: dict | None
    valuation: dict | None
    compliance_verdict: dict | None
    final_memo: str | None
```

**Agents (each a node / subgraph with its own tools):**
- **Supervisor** — no tools. Reads state, routes to next agent, synthesizes final memo. Human-in-the-loop interrupt (LangGraph checkpoint) BEFORE final memo is marked approved.
- **Scout** — tools: `search_players` (SQL over thesis_targets + stats), `compare_players`, `get_player_profile`, `read_watchlist`, `add_to_watchlist`
- **Valuation** — tools: `comparable_transfers` (SQL), `estimate_fee_range`, `estimate_wage_demand`
- **Compliance** — tools: `retrieve_regulations` (RAG over pgvector), `compute_psr_impact` (pure-Python calc over club_finances + book_value), `book_value_lookup`. Every regulatory claim must cite source_doc + article.

**Checkpointing:** LangGraph Postgres checkpointer pointed at Supabase. Runs are resumable; approval flow uses interrupts.

**RAG design (surgical):** RAG is ONLY for regulation text. Structured questions (stats, wages, finances) go to SQL tools. Retrieval = hybrid: pgvector cosine similarity + Postgres full-text search, merged, then reranked. Chunking follows document structure (article/section), not blind token windows.

---

## 6. API Surface (FastAPI)

```
GET  /health                      liveness
POST /runs                        start a graph run {question | player_name, run_type}  → {run_id}
GET  /runs/{id}                   status + current state (poll)
POST /runs/{id}/approve           human approval → resume from interrupt
GET  /squad/efficiency            squad_efficiency view (dashboard)
GET  /targets                     thesis_targets view, filterable
GET  /watchlist                   list watchlist
POST /watchlist                   add player
GET  /freshness                   data_freshness table
```

Long-running pattern: POST /runs returns immediately; graph executes in background task; frontend polls. Auth: static API key header for v1. CORS: allow the Vercel domain.

---

## 7. Phases & Tickets

### Phase 0 — Skeleton, deployed on day one
- **FOC-1** Repo scaffold per layout above; uv; ruff+mypy+pytest wired in GitHub Actions. AC: CI green on a trivial test.
- **FOC-2** FastAPI app with /health; structured JSON logging. AC: runs locally via `docker compose up`.
- **FOC-3** Multi-stage Dockerfile (uv builder → slim runtime, non-root, healthcheck). AC: image <300MB.
- **FOC-4** Deploy to Cloud Run via GitHub Actions (build → GHCR → deploy on merge to main). Env vars via Cloud Run secrets. AC: public URL serves /health.
- **FOC-5** `/docs/architecture.md` v1 with Mermaid diagram (frontend → API/LangGraph container → Supabase spine → external APIs; dotted lines to Langfuse/OTel).

### Phase 1 — Supabase schema + first data in
- **FOC-6** Migrations for all core tables (§4) incl. `watchlist`; enable pgvector extension. AC: migrations idempotent, run via script.
- **FOC-7** Kaggle Transfermarkt importer → players, clubs, contracts, transfers. AC: ≥5k players across top-5 leagues; upserts idempotent; freshness row written.
- **FOC-8** FBref scraper (rate-limited client, disk cache, incremental) → player_stats for Premier League first. AC: full Man United squad + league per-90s for current season.
- **FOC-9** Understat scraper → xG/xA columns merged into player_stats. AC: joined by player mapping table; unmapped players logged, not dropped.
- **FOC-10** Capology scraper → contracts.weekly_wage_est for all Prem squads. AC: ≥90% of Prem players have a wage estimate.
- **FOC-11** Companies House client → club_finances for all 20 Prem clubs, latest filing. AC: revenue + wage_bill populated with source_filing_url.

### Phase 2 — RAG corpus + retrieval
- **FOC-12** Upload UEFA Financial Sustainability + PL PSR PDFs to Supabase Storage; parser → structure-aware chunks (article/section) → regulation_chunks. AC: chunks carry source_doc + article metadata.
- **FOC-13** Embedding pipeline + hybrid retrieval function (vector + FTS + rerank) exposed as internal service AND as LangGraph tool. AC: "can we amortise a fee over 7 years?" surfaces the 5-year amortisation cap rule in top 3.
- **FOC-14** Retrieval eval harness: 25 golden Q→chunk pairs, recall@k in CI. AC: recall@3 ≥ 0.8.

### Phase 3 — First agent (Scout), end to end
- **FOC-15** LLM wrapper module (single entry point for all Anthropic calls; Langfuse hooks land here later). AC: retries, timeout, typed responses.
- **FOC-16** Scout agent (single ReAct graph) with SQL tools + watchlist tools. AC: 10 scripted scouting queries return sensible results; outputs Pydantic-validated.
- **FOC-17** /runs API with background execution + polling; LangGraph Postgres checkpointer on Supabase. AC: full run visible via poll; survives API restart mid-run.

### Phase 4 — Efficiency engine (SQL views)
- **FOC-18** Position-weight config table + `player_performance_index` view. AC: percentiles sane on spot-check (known stars rank high).
- **FOC-19** `player_wage_context` + `squad_efficiency` views with quadrant labels + renewal/offload priority scores. AC: Man United squad renders full quadrant; documented methodology caveats in /docs (wage estimates, volume-stat bias — screening tool, not oracle).
- **FOC-20** `book_value` + `thesis_targets` views. AC: book value math matches hand-calc for 3 known transfers; thesis list passes the sniff test.

### Phase 5 — Full multi-agent system
- **FOC-21** Valuation agent + tools. AC: fee range cites ≥3 comparables.
- **FOC-22** Compliance agent: RAG tool + compute_psr_impact + book_value_lookup. AC: "sign X for €80m/5yr" returns computed numbers + verbatim rule citations.
- **FOC-23** Supervisor graph wiring all agents; human-approval interrupt before final memo; /runs/{id}/approve endpoint. AC: flagship flow "Should we sign [player]?" produces a complete memo end-to-end; graph diagram exported to docs.
- **FOC-24** Structured output hardening: every agent output schema-validated, retry-on-invalid, tool errors surfaced into state not exceptions. AC: chaos test (malformed outputs injected) never 500s.

### Phase 6 — Observability
- **FOC-25** Langfuse integration via callback handler in the LLM wrapper: traces per run, spans per node/tool, cost + latency per agent, sessions per conversation. AC: dashboard answers "which agent costs most per run."
- **FOC-26** Langfuse eval datasets: scout accuracy + compliance citation correctness, LLM-as-judge scorers, run on prompt changes. AC: score per release visible.
- **FOC-27** OpenTelemetry: FastAPI + ingestion instrumented → Grafana Cloud; RED dashboard. AC: one trace spans request → retrieval → graph execution.

### Phase 7 — Frontend dashboard
- **FOC-28** Next.js: chat interface hitting /runs with polling + approval button. AC: full memo flow usable in browser.
- **FOC-29** Squad efficiency quadrant scatter (perf percentile vs wage percentile, sized by contract runway), thesis targets table, watchlist page, freshness footer. AC: deployed to Vercel against the Cloud Run API.

### Phase 8 — Kubernetes learning phase (build, document, tear down)
- **FOC-30** K8s manifests (Deployments api+ingestion, Service, Ingress, probes, ConfigMaps/Secrets) validated on kind locally.
- **FOC-31** Terraform: EKS or AKS cluster; deploy via Actions; HPA + resource limits; load test (locust, 50 concurrent) documented with p95. AC: screenshots + write-up in /docs, then cluster destroyed.

### Phase 9 — Stretch (in priority order)
- **FOC-32** A2A extraction: compliance agent as standalone container with Agent Card; supervisor calls it over A2A; OTel distributed trace across both services.
- **FOC-33** CrewAI comparison: rebuild scout flow, 1-page honest write-up.
- **FOC-34** Signals worker (the deferred sentiment feature): daily news pull for watchlist players → LLM event classification → player_signals table → opportunity briefs when signal + thesis align.
- **FOC-35** FDE artifact pack: discovery doc, scoping proposal, 8–10 min demo video, post-mortem blog post.

---

## 8. Out of Scope (v1)
- Real-time/streaming data of any kind
- Sentiment/news ingestion (Phase 9 only)
- Multi-club tenancy, user accounts beyond basic auth
- Scraping Transfermarkt directly (ToS — use Kaggle dataset)
- Any second vector database — pgvector only

## 9. Definition of Done (per phase)
- All ACs met, CI green, deployed to Cloud Run (from Phase 0 onward, main is always live)
- /docs/architecture.md updated
- Freshness visible for any new data source
