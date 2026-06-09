# Enterprise Adoption & Escalation Risk Analyzer

**An applied-AI tool that scores every customer account on a single, explainable
`0–100` escalation-risk number** — by fusing support, infrastructure-health,
customer-success, renewal, and internal-engagement signals — then drafts the
outreach and a **Claude-written action plan** to do something about it.

It is the customer-success / solutions-architecture job, written in code:
*find the accounts about to churn or escalate, explain exactly why, and hand
the owner the next move.*

[**▶ Live demo**](https://enterprise-adoption-risk-analyzer.onrender.com) ·
runs on 100% synthetic data, no login required.

![mode](https://img.shields.io/badge/mode-demo-blue)
![data](https://img.shields.io/badge/data-synthetic-green)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![framework](https://img.shields.io/badge/FastAPI-async-009688)
![ai](https://img.shields.io/badge/AI-Claude%20advisory-d97757)
![license](https://img.shields.io/badge/license-MIT-lightgrey)

> [!IMPORTANT]
> **This is a personal portfolio demo.** It runs entirely on **synthetic,
> randomly-generated data** — every company name, metric, support case, cluster,
> and contact is fictional. It is **not affiliated with, endorsed by, or
> connected to any employer or vendor**, contains **no proprietary code paths,
> credentials, or customer data**, and connects to **no live systems**. Any
> resemblance to a real organisation is coincidental.

---

## Contents

- [Why I built this](#why-i-built-this)
- [What it does](#what-it-does)
- [The Claude advisory layer](#the-claude-advisory-layer)
- [How the risk score works](#how-the-risk-score-works)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Deploy](#deploy)
- [Architecture](#architecture)
- [Project structure](#project-structure)
- [API reference](#api-reference)
- [Demo data](#demo-data)
- [Demo vs. production](#demo-vs-production)
- [Tech stack](#tech-stack)
- [License & disclaimer](#license--disclaimer)

---

## Why I built this

I spend my days as a technical, customer-facing operator: keeping enterprise
accounts healthy, catching escalations before they blow up, and explaining
*why* a customer is at risk to people who need to act on it. The hard part was
never any single dashboard — it was that the signal lives in five different
systems and no one number tells you where to look first.

So I built one. This project takes the messy, multi-system reality of account
health and collapses it into **one explainable score per account**, with the
full factor-level breakdown behind it, and then uses an LLM to turn that
breakdown into a concrete plan. It's a compact demonstration of the work I care
about: **applied AI that sits on real enterprise signals, stays explainable, and
degrades gracefully** — never a black box, never a hard dependency on the model
being available.

## What it does

- **Unified risk score** — a weighted engine merges ~15 signals across five
  source categories into one explainable `0–100` score per account.
- **Full explainability** — every score decomposes into ranked risk *factors*
  with each source's contribution (support / infra / CS / engagement / manual
  CXM flags), shown as a donut + factor list. No score is unaccountable.
- **AI Advisory (Claude)** — turns an account's risk profile into a prioritized,
  one-week action plan, server-side, with a zero-cost deterministic fallback.
  See [below](#the-claude-advisory-layer).
- **Portfolio views** — a Top-Risks leaderboard, region filters (APJ / EMEA /
  AMER), and a per-CXM book of business ("My Accounts").
- **Account drill-downs** — infrastructure (cluster / version / EOL / hypervisor
  charts), incident tickets, license & adoption, renewal proximity, internal
  engagement intel, and manual CXM risk flags (full CRUD).
- **AI-assisted outreach** — auto-drafts a customer email summarising the flagged
  risks and recommended next steps, with copy / mailto delivery.
- **Semantic search** — find similar accounts and free-text search over the
  portfolio (vector store optional, degrades gracefully when absent).
- **Background ingestion model** — an APScheduler loop and a `/sync` monitor page
  mirror how the production version continuously refreshes signals.
- **CSV export** of the full scored portfolio.

## The Claude advisory layer

The **AI Advisory** tile in each account view turns that account's risk profile
into a crisp, prioritized action plan a Customer Experience Manager can act on
this week.

It is designed to be **honest, safe, and cost-controlled**:

| Property | How it works |
|---|---|
| **Server-side only** | The Anthropic call happens in the FastAPI backend (`app/engine/claude_advisor.py`). The API key is read from the environment and **never reaches the browser**. |
| **Graceful fallback** | If no `ANTHROPIC_API_KEY` is set (or the call fails), a **deterministic advisory** is synthesised locally from the same signals. The feature always works, even with no key and no network. |
| **Cost-controlled** | Claude is invoked **only on an explicit click**, never on page load, and results are **cached per account** — so it's called at most once per account. With a Haiku/Sonnet model that's a fraction of a cent per account. |
| **Grounded** | The prompt contains only the account's computed score, top weighted factors, and recommendations — no PII, no invented data — and asks for an action plan grounded strictly in those signals. |

The response is tagged in the UI as either *"Generated by Claude · {model}"* or
*"Deterministic advisory"* so it's always clear what produced it.

```
risk profile ──► claude_advisor.generate_advisory()
                      │
        ANTHROPIC_API_KEY set? ──► yes ──► Anthropic Messages API ──► action plan ("source": "claude")
                      │
                      └────────── no / error ──► local deterministic plan ("source": "deterministic")
```

## How the risk score works

Each connector contributes normalised signals; the scorer applies a fixed weight
to each and sums them into the `0–100` score, then buckets it into
`low / medium / high / critical`. Every contribution is retained so the UI can
show *why* an account scored the way it did.

| Signal | Weight | Category |
|---|---|---|
| Open case volume | 10% | Support |
| P1/P2 severity ratio | 13% | Support |
| Escalation history | 13% | Support |
| Case aging (>14 days) | 7% | Support |
| Repeat issues | 5% | Support |
| Critical alerts | 10% | Infrastructure |
| Unhealthy clusters | 8% | Infrastructure |
| Health-check failures | 4% | Infrastructure |
| Contract status | 5% | Infrastructure |
| Telemetry connectivity | 4% | Infrastructure |
| End-of-life exposure | 6% | Infrastructure |
| CS health score | 5% | Customer Success |
| Engagement score | 3% | Customer Success |
| Renewal proximity | 4% | Customer Success |
| Sentiment / NPS | 3% | Customer Success |

Manual **CXM risk flags** can be layered on top per account. Weights live in
`app/engine/risk_scorer.py`.

## Quickstart

No configuration or API keys needed — it boots straight into demo mode.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open <http://localhost:8000>. On first boot the app seeds a fictional
15-account portfolio into a local SQLite DB and pre-computes every risk profile,
so all views are populated immediately.

Using the `Makefile`:

```bash
make install   # create venv + install deps
make run       # run the dev server
make docker-build && make docker-run
```

## Configuration

All configuration is via environment variables (or a local `.env` — see
`.env.example`). Everything is optional; the defaults run a fully working demo.

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `true` | Forces synthetic generators and inert connectors. Keep `true` for the public build. |
| `ANTHROPIC_API_KEY` | _(unset)_ | Enables live Claude advisories. When unset, the deterministic fallback is used (zero cost). |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Model used for advisories. A Haiku model is cheaper if you prefer. |
| `PORT` | `8000` | Bind port for the deploy start command (`uvicorn … --port $PORT`). `run.py` uses `8000` directly. |

> The key is read **server-side only** and is never exposed to the client.

## Deploy

It's a standard ASGI app (`app.main:app`):

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Works as-is on **Render / Railway / Fly.io** or any container. A
[`render.yaml`](render.yaml) blueprint and a [`Dockerfile`](Dockerfile) are
included. The live demo runs on Render's free tier (the first request after idle
may take a moment to wake). SQLite + the optional vector store live under
`data/` and are re-seeded on boot, so an ephemeral disk is fine for the demo.

To enable live Claude advisories on a deployment, set `ANTHROPIC_API_KEY` (and
optionally `CLAUDE_MODEL`) in the host's environment settings.

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│            Dashboard (HTML / vanilla JS / Chart.js)         │
│   Risk table · Portfolio donut · Drill-downs · AI Advisory  │
│                · Email composer · CSV export                │
├───────────────────────────────────────────────────────────┤
│                      FastAPI backend                        │
├──────────────┬──────────────┬──────────────┬───────────────┤
│  Aggregator  │ Risk Scorer  │ Claude        │  Email Gen    │
│ (orchestr.)  │ (weights)    │ Advisor       │               │
├──────────────┴──────────────┴──────────────┴───────────────┤
│   Connector layer (demo: synthetic generators)             │
│   support · infra/monitoring · CS platform · engagement     │
├───────────────────────────────────────────────────────────┤
│   SQLite (profiles, snapshots, aggregates) · vector store*  │
│   APScheduler (background recompute) · TTL cache            │
└───────────────────────────────────────────────────────────┘
* vector store is optional and degrades gracefully when absent
```

In the production version the connector layer reads from real systems on a
scheduled ingest. **In this public build those connectors are inert**: the
heavy / live-only dependencies (browser automation, the CRM SDK, the embeddings
model) are not installed, the connector imports are guarded, and every read
falls back to a deterministic synthetic generator.

## Project structure

```
enterprise-adoption-risk-analyzer/
├── run.py                     # dev entrypoint
├── render.yaml · Dockerfile · Makefile   # deploy
├── requirements.txt · .env.example
└── app/
    ├── main.py                # FastAPI app + all routes
    ├── config.py              # pydantic-settings configuration
    ├── engine/
    │   ├── aggregator.py      # orchestrates connectors → profiles → aggregates
    │   ├── risk_scorer.py     # weighted scoring + factor breakdown
    │   ├── claude_advisor.py  # Claude advisory + deterministic fallback
    │   ├── email_generator.py # outreach email drafting
    │   └── region_map.py
    ├── connectors/            # data sources (demo: synthetic generators)
    │   ├── demo_data.py       # synthetic portfolio + signal generators
    │   ├── salesforce.py · nutanix_insights.py · csinsights.py
    │   ├── planhat.py · glean.py · outlook.py · portal_public.py …
    ├── sync/                  # background ingestion + scheduler
    │   ├── scheduler.py · base.py · event_bus.py · *_sync.py
    ├── store/                 # persistence
    │   ├── db.py · custom_risks.py · vector_store.py
    ├── static/                # css/style.css · js/app.js
    └── templates/             # dashboard.html · sync.html
```

## API reference

A curated set of the JSON endpoints (full list in `app/main.py`):

**Core**

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `GET` | `/sync` | Ingestion / freshness monitor page |
| `GET` | `/api/health` · `/api/status` | Liveness + runtime status |

**Portfolio**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/top-risks?limit=20` | Top at-risk accounts |
| `GET` | `/api/accounts?offset=0&limit=50` | Paged account index |
| `GET` | `/api/accounts/search?q=` | Fast type-ahead search |
| `GET` | `/api/regions/counts` | Per-region account counts |
| `GET` | `/api/my-accounts?username=aria.menon` | Per-CXM book of business |
| `GET` | `/api/cxm/roster` | CXM roster (optionally by region) |
| `GET` | `/api/portfolio/risk-breakdown?level=high` | Portfolio risk donut |
| `GET` | `/api/export/csv` | Export the scored portfolio |

**Single account**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/account/{id}?account_name=...` | Full risk analysis for one account |
| `GET` | `/api/account/{id}/ai-advisory` | **Claude action plan** (deterministic fallback) |
| `GET` | `/api/account/{id}/email-draft` | Generate outreach email draft |
| `GET` | `/api/account/{id}/risk-breakdown` | Score breakdown by source |
| `GET` | `/api/account/{id}/similar` | Semantically similar accounts |
| `GET` | `/api/licenses/{id}` | License / adoption analysis |
| `GET` | `/api/infrastructure/{id}` | Cluster / version / EOL summary |

**Manual CXM risk flags**

| Method | Path | Description |
|---|---|---|
| `GET` · `POST` | `/api/account/{id}/custom-risks` | List / add manual risk flags |
| `PUT` · `DELETE` | `/api/account/{id}/custom-risks/{risk_id}` | Update / remove a flag |
| `GET` | `/api/risk-categories` | Available flag categories |

**Ingestion (demo: synthetic)**

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/sync/runs` · `/api/sync/freshness` | Recent runs + staleness |
| `POST` | `/api/sync/run/{source}` | Trigger a batch sync for one source |
| `POST` | `/api/cache/clear` | Clear the TTL cache |

## Demo data

On first run, `app/connectors/demo_data.py` seeds a deterministic, fictional
portfolio of **15 enterprise accounts** spread across **APJ / EMEA / AMER**,
each assigned a region, industry, owner, and CXM. Every signal — support cases,
cluster health, EOL exposure, adoption, renewal dates, engagement touchpoints —
is generated synthetically and then scored through the same engine the
production build uses. Because seeding is deterministic, the demo looks the same
on every boot and the Claude/deterministic advisory for a given account is
stable (and cacheable).

## Demo vs. production

| | Demo (this repo) | Production |
|---|---|---|
| Data | Synthetic generators | Live CRM / monitoring / CS systems |
| Connectors | Inert, import-guarded | Scheduled ingest from real APIs |
| Heavy deps | Not installed | Browser automation, CRM SDK, embeddings |
| Secrets | None | Managed credentials |
| Purpose | Public portfolio piece | Internal operational tool |

## Tech stack

Python 3.11+ · FastAPI · Uvicorn · Pydantic v2 · APScheduler · SQLite · Jinja2 ·
Chart.js · vanilla JS · Anthropic Messages API (optional).

## License & disclaimer

MIT — see [`LICENSE`](LICENSE).

This is an independent portfolio project built on synthetic data to demonstrate
applied-AI and customer-success engineering. It is not affiliated with any
employer or vendor and contains no proprietary or customer data.
