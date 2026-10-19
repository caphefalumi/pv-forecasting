# System Architecture & Blueprint

| | |
|---|---|
| **Project** | COS40007 Applied Project — Day-Ahead Solar PV Energy Forecasting |
| **System Scope** | End-to-End MLOps Platform (Data Pipeline, Model Lifecycle, Cloud Serving, Drift Observability) |
| **Cloud Stack** | AWS (VPC, ALB, ECS Fargate, ECR, S3, Step Functions, EventBridge), Supabase, Vercel, Grafana Cloud |

---

## 1. High-Level System Overview

The platform is an end-to-end MLOps system that forecasts the 24-hour solar photovoltaic energy production (kWh) of the next day (**D+1**) and sums it into a daily total. The target is normalized by each array's installed capacity ($\text{kWp}$), enabling a single fleet LSTM model to serve newly installed customer arrays of arbitrary size, tilt, azimuth, tracking configuration, and cell technology.

```text
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│                                 1. DATA & ML PIPELINE                                     │
│                                                                                           │
│  [DKASC 5-min PV Labels] ──┐                                                              │
│  [Open-Meteo D+1 NWP]   ───┼──► [solar-ingest] ──► [S3 + DVC Versioned Dataset]           │
│  [Array Metadata CSVs]  ───┘          │                        │                          │
│                                       ▼                        ▼                          │
│                               [solar-drift (KS/PSI)]   [solar-retrain (LSTM)]             │
│                                       │                        │                          │
│                                       ▼                        ▼                          │
│                             [S3 Drift Reports (.md)]   [Challenger vs Champion]           │
│                                                                │                          │
│                                                      [Human Approval Gate]                │
│                                                                │                          │
│                                                                ▼                          │
│                                                    [MLflow Model Registry]                │
└────────────────────────────────────────────────────────────────┼──────────────────────────┘
                                                                 │ @champion alias
┌────────────────────────────────────────────────────────────────┼──────────────────────────┐
│                              2. PREDICTION SERVING LAYER       ▼                          │
│                                                                                           │
│  [Vercel Frontend UI] ──HTTPS──► [AWS Public ALB] ──► [ECS Fargate: solar-api (Private)]  │
│  (TanStack Start)                (Public Subnet)               │                          │
│                                         ┌──────────────────────┼────────────────────┐     │
│                                         ▼                      ▼                    ▼     │
│                                [Open-Meteo API]     [Supabase PostgreSQL]   [Grafana Cloud]│
│                                (Live D+1 Weather)   (prediction_logs DB)    (Prom/Loki)   │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Core Subsystems

### 2.1 Data & Feature Pipeline (`pvdata/` & `jobs/ingest_pipeline/`)
- **Ground-Truth Labels:** 5-minute active power records from 46 arrays at the Desert Knowledge Australia Solar Centre (DKASC) in Alice Springs (31 usable arrays after excluding faulty meters and unverified trackers: 25 training arrays + 6 held-out cold-start arrays).
- **Weather Inputs:** Day-ahead numerical weather prediction (NWP) runs from the Open-Meteo Previous Runs API (`icon_seamless` through 2024-12-31, `ecmwf_ifs025` from 2025-01-01 onward). Observed station weather is strictly excluded from model features to prevent data leakage and is retained solely for monitoring error attribution.
- **Physics Feature Engineering:** Computes solar zenith/azimuth, Ineichen clear-sky irradiance (`ghi_clear`), Hay-Davies plane-of-array irradiance (`poa_fc`), SAPM cell temperature (`temp_cell_fc`), and physical baseline power (`p_phys_norm`) via `pvlib`.
- **Versioning:** Processed snapshots and monthly CSV exports (`exports/YYYY-MM.csv.dvc`) are versioned with DVC and backed by S3-compatible object storage.

### 2.2 Model Training & Registry (`jobs/retrain_pipeline/` & `promote.py`)
- **Fleet Model:** 2-layer PyTorch LSTM (`hidden_size=128`, `dropout=0.2`, 6-hour sliding window over 23 base features) paired with a `scikit-learn` preprocessing pipeline (`SimpleImputer` + `StandardScaler`).
- **Experiment Tracking & Registry:** MLflow Server running on ECS Fargate in the private subnet, backed by Supabase PostgreSQL for metadata and S3/Supabase Storage (`artifacts/<run_id>/checkpoint.pt`) for model weights.
- **Champion / Challenger Lifecycle:** Newly trained models are evaluated against the active `@champion` and tested on behavioral golden sets. Qualifying models receive the `@candidate` alias and require human approval (`python promote.py`) before transitioning to `@champion`.

### 2.3 Prediction Serving (`apps/backend/` & `apps/frontend/`)
- **Frontend (`apps/frontend/`):** React 19 + TanStack Router/Query/Form SPA hosted on Vercel. Users input their array parameters (`kwp`, `technology`, `tracking`, `tilt_deg`, `surface_azimuth_pvlib`, `predict_date`) and view the 24-hour D+1 generation curve, peak hour, specific yield ($\text{kWh}/\text{kWp}$), and daily total.
- **Public Entry Point (`infra/terraform/modules/alb/`):** AWS Application Load Balancer in the public subnet is the sole internet-facing entry point, forwarding traffic to port `8000` in the private subnet.
- **Backend (`apps/backend/`):** FastAPI service running on ECS Fargate (`solar-api`). Fetches the D+1 weather forecast from Open-Meteo, builds the 24-hour feature tensor, runs batch daytime inference (`06:00–18:00`) using the active `@champion` model, checks for Out-of-Distribution (`is_ood`) inputs, and logs every prediction to Supabase `prediction_logs`.

### 2.4 Orchestration & Observability (`infra/terraform/modules/pipeline_scheduler/` & `infra/monitoring/`)
- **Scheduled Batch Jobs:** Amazon EventBridge Scheduler and AWS Step Functions (`ecs:runTask.sync`) coordinate four containerized jobs sharing the root pipeline image:
  - `solar-ingest` (Daily 01:00 UTC)
  - `solar-evaluate` (Daily 03:00 UTC)
  - `solar-drift` (Daily 04:00 UTC)
  - `solar-retrain` (Weekly Sunday 02:00 UTC or triggered early on sustained degradation)
- **Multi-Layer Monitoring:** CloudWatch Container Insights (infrastructure), Prometheus + Grafana Cloud (service latency/errors, OOD rate, cohort nMAE, error attribution $e_{\text{model}}$ vs. $e_{\text{forecast}}$, and feature drift scores), and SQS Dead-Letter Queue (`pipeline-dlq`) for batch failure resilience.

---

## 3. End-to-End Lineage & Traceability

Every prediction served to a user can be audited back to the exact code, dataset snapshot, and training run that produced it:

| Artifact | Versioning System | Identifier |
|---|---|---|
| **Source Code** | Git / GitHub | Commit SHA |
| **Container Image** | Amazon ECR | Image tag = Git commit SHA |
| **Dataset Snapshot** | DVC + S3 | MD5/SHA-256 hash in `exports/*.csv.dvc` committed to Git |
| **Trained Model** | MLflow Model Registry | `run_id` + registered model version (`@champion` alias) |
| **Served Prediction** | Supabase `prediction_logs` | `model_version` + `X-Request-ID` + input/weather payload |

---

## 4. Network & Security Boundary

- **VPC Isolation:** Public subnets host only the Application Load Balancer and NAT Gateway. All compute tasks (`solar-api`, `mlflow-server`, `solar-ingest`, `solar-evaluate`, `solar-drift`, `solar-retrain`) run in private subnets with `assign_public_ip = false`.
- **Least-Privilege IAM:** Separate ECS Task Execution Role (ECR image pull, CloudWatch logs) and ECS Task Role (scoped S3 bucket read/write). Runtime secrets come from GitHub Actions → ECS task environment.
- **Short-Lived CI Credentials:** GitHub Actions authenticates to AWS via OpenID Connect (OIDC) without storing static AWS access keys.
- **Hardened Containers:** Both `Dockerfile` (pipeline) and `apps/backend/Dockerfile` (FastAPI) use multi-stage builds and execute as an unprivileged non-root user (`UID 10001`, `appuser`).
