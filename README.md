# Day-Ahead Solar PV Energy Forecasting Platform

An end-to-end MLOps and cloud-native forecasting platform that predicts 24-hour photovoltaic (PV) solar energy generation (**kWh**) for the next day (**D+1**) and aggregates the daily total.

Ground-truth labels are sourced from 46 solar arrays (31 usable: 25 training + 6 held-out cold-start) at the **Desert Knowledge Australia Solar Centre (DKASC)** in Alice Springs, paired with day-ahead numerical weather prediction (NWP) runs from **Open-Meteo Previous Runs**, `pvlib` solar physics features, a **PyTorch LSTM** fleet model, **MLflow** Champion/Challenger lifecycle management, **FastAPI** serving on **AWS ECS Fargate**, **React 19 + TanStack Router** SPA on **Vercel**, **Supabase** prediction logging, and **Grafana Cloud** observability.

---

## 1. System Architecture

```text
[DKASC 5-min Power] + [Open-Meteo D+1 NWP] + [Array Metadata]
        │
        ▼
[solar-ingest (pvdata + DVC)] ──► [S3 / Supabase Storage + .dvc Pointers]
        │                                       │
        ├──► [solar-drift (KS / PSI)]           ├──► [solar-retrain (PyTorch LSTM)]
        │           │                           │               │
        │           ▼                           │               ▼
        │    [S3 Drift Reports (.md)]           │    [Challenger vs Champion + Golden Set]
        │                                       │               │
        ▼                                       ▼               ▼ (Human Approval Gate)
[solar-evaluate (Daily nMAE/RMSE)] ◄──── [MLflow Model Registry (@champion / @candidate)]
                                                        │
                                                        ▼
[Vercel Frontend (React SPA)] ──HTTPS──► [AWS Public ALB] ──► [ECS Fargate: solar-api (FastAPI)]
                                                                        │
                                               ┌────────────────────────┼────────────────────────┐
                                               ▼                        ▼                        ▼
                                      [Open-Meteo API]         [Supabase DB]            [Grafana Cloud]
                                     (Live D+1 Forecast)     (prediction_logs)        (Prom / Loki / Tempo)
```

---

## 2. Repository Structure

```text
├── .github/workflows/
│   ├── ci.yml                      # Backend pytest, Frontend bun:test + build, Ansible & Terraform checks
│   ├── deploy-backend.yml          # AWS OIDC -> ECR image push -> ECS rolling deploy -> ALB smoke test
│   ├── promote-model.yml           # Manual workflow to promote MLflow @candidate -> @champion
│   ├── terraform-aws.yml           # Terraform fmt, validate, plan & apply for AWS stack
│   └── terraform-supabase.yml      # Terraform validate, plan & apply for Supabase stack
├── apps/
│   ├── backend/                    # FastAPI D+1 prediction service, Prometheus /metrics, MLflow loader
│   └── frontend/                   # React 19 + TanStack Router/Query/Form SPA (Vercel)
├── config/
│   └── pipeline.yaml               # Data pipeline configuration (QC, physics, time/array splits, recipes)
├── data/
│   ├── arrays_metadata.csv         # Specifications for all 46 DKASC arrays (committed)
│   ├── array_overrides.csv         # Curated array validity overrides (committed)
│   └── dkasc_api_5min/
│       └── source_names.csv        # DKASC API column dictionary (committed)
├── docs/
│   ├── architecture.md             # System blueprint, network isolation, and artifact lineage
│   ├── api-contract.md             # FastAPI endpoint specs, headers, OOD flags, and Supabase schema
│   ├── data-pipeline.md            # Complete pvdata feature engineering, QC, and DVC reference
│   ├── deployment-and-monitoring.md# CI/CD workflows, ECS tasks, Step Functions, and Grafana alerting
│   └── mlops-lifecycle.md          # Splits, LSTM training, drift detection, and human-approved promotion
├── exports/                        # DVC pointer files (*.csv.dvc) tracking monthly datasets in S3
├── infra/
│   ├── monitoring/                 # Grafana dashboard JSON provisioning
│   ├── monitoring/grafana/         # Provisioned Grafana Cloud dashboards (Model/DQ & Infra/Service)
│   └── terraform/                  # 8 modular AWS Terraform stacks + Supabase stack + dev/prod tfvars
├── jobs/                           # Containerized MLOps pipelines (ingest, evaluate, drift_check, retrain)
├── pvdata/                         # Shared preprocessing, QC, pvlib physics, and split loader package
├── scripts/
│   └── run_weekly.sh               # Weekly production snapshot automation script
├── tests/                          # Data pipeline, physics feature, model, and API integration tests
├── ingest.py                       # CLI entrypoint: data ingestion + DVC + Supabase S3 export
├── evaluate.py                     # CLI entrypoint: live @champion evaluation
├── drift.py                        # CLI entrypoint: KS + PSI covariate & target drift detection
├── retrain.py                      # CLI entrypoint: fleet LSTM retraining & candidate registration
├── promote.py                      # CLI entrypoint: human-approved @candidate -> @champion promotion
├── Dockerfile                      # Multi-stage non-root container for MLOps pipeline & batch jobs
├── package.json                    # Monorepo scripts for frontend, backend, Terraform, and smoke tests
└── requirements.txt                # Python dependencies for data & MLOps pipelines
```

---

## 3. Quick Start

### 3.1 FastAPI Backend (`apps/backend`)
```bash
cd apps/backend
uv sync --dev
uv run uvicorn main:app --reload --port 8000

# Run backend unit & integration tests (24 tests)
uv run pytest -v
```

### 3.2 Web Frontend (`apps/frontend`)
```bash
cd apps/frontend
bun install
bun run dev

# Run frontend unit & integration tests, strict typecheck, and production build
bun run test
bun x tsc --noEmit
bun run build
```

### 3.3 Data & Feature Pipeline (`pvdata`)
```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1 | Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

# Run pipeline & feature test suite
pytest -q
```

### 3.4 Infrastructure & Smoke Verification (`infra`)
```bash
# Run cross-platform architecture & endpoint smoke verification
bun run smoke

# Validate Terraform modular stacks
bun run tf:validate
```

---

## 4. MLOps Pipelines & CLI Commands

### 4.1 Scheduled Batch Jobs (`jobs/`)

| Command | ECS Task Definition | Description |
|---|---|---|
| `python ingest.py [--until YYYY-MM-DD] [--dry-run]` | `solar-ingest` | Fetches DKASC & Open-Meteo data, builds validated snapshot, exports monthly CSVs to Supabase/S3, and updates DVC pointers. |
| `python evaluate.py [--dry-run]` | `solar-evaluate` | Evaluates the active `@champion` model on recent ground-truth records, logs metrics to MLflow, and exits with code `2` if retrain is needed. |
| `python drift.py [--dry-run]` | `solar-drift` | Runs per-feature KS tests and target PSI (7-day current vs. 30-day reference daytime window), logging `.md` drift reports to MLflow and S3. |
| `python retrain.py [--dry-run]` | `solar-retrain` | Trains a Challenger fleet LSTM model, compares against `@champion`, uploads `checkpoint.pt`, and assigns `@candidate` if improved. |
| `python promote.py [--version N] [--approver USER]` | GitHub Action / CLI | Promotes a verified `@candidate` model to `@champion` in MLflow Registry with approver provenance and Discord notification. |

### 4.2 Data Pipeline Subcommands (`python -m pvdata <cmd>`)

| Subcommand | Arguments | Description |
|---|---|---|
| `run` | `[--until YYYY-MM-DD] [--skip-fetch] [--overwrite]` | End-to-end fetch, build, validation, and `latest.json` update. |
| `fetch-dkasc` | `[--start YYYY-MM-DD] [--end YYYY-MM-DD]` | Fetches incremental 5-min DKASC power records in 3-day chunks. |
| `fetch-meteo` | `[--start YYYY-MM-DD] [--end YYYY-MM-DD]` | Fetches day-ahead weather forecasts from Open-Meteo Previous Runs. |
| `build` | `--version <ver> [--end YYYY-MM-DD] [--overwrite]` | Builds a versioned 40-column training table under `data/processed/<ver>/`. |
| `validate` | `--version <ver>` | Runs hard schema/leakage checks and soft sanity checks on a snapshot. |
| `sample` | `--version <ver> --start YYYY-MM-DD --end YYYY-MM-DD` | Exports a human-readable slice to CSV and Excel (`sample.xlsx`). |

---

## 5. Loading Leakage-Free Splits in Python

```python
from pvdata import load_split

# Load pre-registered chronological & array split (25 train arrays, 6 cold-start held out)
split = load_split("fleet_v1")

X_train, y_train = split.X_train, split.y_train
X_val, y_val     = split.X_val, split.y_val
X_test, y_test   = split.X_test, split.y_test
```

| Recipe | Train Period | Validation Period | Test Period | Scope |
|---|---|---|---|---|
| `fleet_v1` | 2024-01-20 → 2024-12-31 | 2025-01-01 → 2025-04-30 | 2025-05-01 → 2025-10-01 | 25 training arrays (6 cold-start held out) |
| `fleet_v2` | 2024-01-20 → 2025-12-31 | 2026-01-01 → 2026-04-30 | 2026-05-01 → 2026-10-01 | 25 training arrays (6 cold-start held out) |
| `v1_vs_v2` | — | — | 2026-05-01 → 2026-10-01 | Common benchmark window for Champion vs. Challenger |
| `rolling` | Local dates `< test_start` | — | Past `test_days` up to `as_of` | Expanding window for production retraining |

---

## 6. Documentation Index (`docs/`)

- [**System Architecture & Blueprint** (`docs/architecture.md`)](docs/architecture.md) — End-to-end architecture, VPC isolation, and artifact lineage chain.
- [**Prediction Serving & API Contract** (`docs/api-contract.md`)](docs/api-contract.md) — FastAPI endpoints, request/response schemas, correlation headers, OOD detection, and Supabase logging.
- [**Data & Feature Pipeline Reference** (`docs/data-pipeline.md`)](docs/data-pipeline.md) — Detailed specification of DKASC & Open-Meteo ingestion, 5-minute & daily QC rules, `pvlib` physics formulas, and DVC snapshots.
- [**MLOps Lifecycle & Retraining Strategy** (`docs/mlops-lifecycle.md`)](docs/mlops-lifecycle.md) — Two-axis splitting, nMAE metric, hybrid retraining triggers, Challenger vs. Champion comparison, and human-approved promotion/rollback.
- [**Deployment, CI/CD & Monitoring Runbook** (`docs/deployment-and-monitoring.md`)](docs/deployment-and-monitoring.md) — GitHub Actions workflows, ECS Fargate task definitions, AWS Step Functions state machines, and 4-layer Grafana Cloud observability.

---

## 7. Data Licence Note

DKASC Alice Springs measurement data is published by Desert Knowledge Australia under copyright. Raw measurement CSVs, API extracts, and derived power datasets are used strictly for educational and research purposes within COS40007 and are excluded from Git via `.gitignore` and tracked remotely via DVC.
