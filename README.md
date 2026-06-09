# Adoption & Escalation Risk Analyzer — Demo

A FastAPI + vanilla-JS dashboard that scores customer accounts on a single
**0–100 escalation-risk** number by fusing signals from multiple systems
(support cases, infrastructure health, customer-success metrics, renewal
proximity, and internal engagement), then drafts an outreach email with the
top problems and recommendations.

> **This is a personal portfolio demo.** It runs entirely on **synthetic,
> randomly-generated data** — every company name, metric, case, and contact
> is fictional. It is **not affiliated with, endorsed by, or connected to any
> employer or vendor**, contains **no proprietary code paths, credentials, or
> customer data**, and connects to **no live systems**. Any resemblance to a
> real organisation is coincidental.

![demo](https://img.shields.io/badge/mode-demo-blue) ![data](https://img.shields.io/badge/data-synthetic-green) ![python](https://img.shields.io/badge/python-3.11%2B-blue)

---

## What it does

- **Unified risk score** — a weighted engine merges ~15 signals across five
  source categories into one explainable 0–100 score per account.
- **Explainability** — every score breaks down into ranked risk *factors* with
  the contribution of each source (support / infra / CS / engagement / manual
  CXM flags), shown as a donut + factor list.
- **Portfolio views** — Top Risks leaderboard, region filters, and a
  per-CXM portfolio ("My Accounts").
- **Account drill-down** — infrastructure (cluster/version/EOL/hypervisor
  charts), incidents, licenses, renewal, engagement intel, and custom risk
  flags.
- **AI-assisted outreach** — auto-drafts a customer email summarising the
  flagged risks and recommended next steps, with copy / mailto delivery.
- **AI Advisory (Claude)** — an account drill-down panel that turns the risk
  profile into a prioritized action plan. Calls the Anthropic API server-side
  when `ANTHROPIC_API_KEY` is set (the key never reaches the browser); falls
  back to a deterministic advisory at zero cost otherwise. Claude is invoked
  only on explicit click and cached per account.
- **CSV export** of the scored portfolio.

## Run it locally

No configuration or API keys needed — it boots straight into demo mode.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Open <http://localhost:8000>. On first boot the app seeds a fictional
15-account portfolio into a local SQLite DB and pre-computes every risk
profile so all views are populated immediately.

## Deploy

It's a standard ASGI app (`app.main:app`). Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Works as-is on Render / Railway / Fly.io / a container. A `render.yaml` and a
`Dockerfile` are included. SQLite + ChromaDB live under `data/` and are
re-seeded on boot, so ephemeral disks are fine for the demo.

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│            Dashboard (HTML / vanilla JS / Chart.js)         │
│     Risk table · Portfolio donut · Drill-downs · Composer   │
├───────────────────────────────────────────────────────────┤
│                      FastAPI backend                        │
├───────────────┬───────────────┬───────────────┬────────────┤
│  Aggregator   │  Risk Scorer  │  Email Gen     │ TTL Cache  │
├───────────────┴───────────────┴───────────────┴────────────┤
│   Connector layer (demo: synthetic generators)             │
│   support · infra/monitoring · CS platform · engagement     │
├───────────────────────────────────────────────────────────┤
│   SQLite (profiles, snapshots, aggregates) · ChromaDB*      │
│   APScheduler (background recompute)                        │
└───────────────────────────────────────────────────────────┘
* vector store is optional and degrades gracefully when absent
```

In the production version, the connector layer reads from real systems on a
scheduled ingest. **In this public build those connectors are inert**: the
heavy/live-only dependencies (browser automation, the CRM SDK, the embeddings
model) are not installed, the connector imports are guarded, and every read
falls back to a deterministic synthetic generator.

## Risk scoring (weights)

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

Weights live in `app/engine/risk_scorer.py`.

## Selected API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `GET` | `/api/top-risks?limit=20` | Top at-risk accounts |
| `GET` | `/api/account/{id}?account_name=...` | Full risk analysis for one account |
| `GET` | `/api/account/{id}/email-draft` | Generate outreach email draft |
| `GET` | `/api/licenses/{id}` | License / adoption analysis |
| `GET` | `/api/my-accounts?username=aria.menon` | Per-CXM portfolio |
| `GET` | `/api/portfolio/risk-breakdown?level=high` | Portfolio risk donut |
| `GET` | `/api/export/csv` | Export the scored portfolio |
| `GET` | `/api/status` · `/api/health` | Runtime + health |

## Tech

Python 3.11+ · FastAPI · Uvicorn · Pydantic v2 · APScheduler · SQLite ·
Jinja2 · Chart.js · vanilla JS.

## License

MIT — see `LICENSE`.
