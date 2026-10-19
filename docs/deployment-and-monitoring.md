# Deployment, CI/CD & Monitoring Runbook

| | |
|---|---|
| **CI/CD Engine** | GitHub Actions (`.github/workflows/`) with AWS OIDC Authentication |
| **Infrastructure as Code** | Terraform (`infra/terraform/`) + Supabase Remote S3 State |
| **Observability Stack** | AWS CloudWatch Container Insights + Prometheus + Grafana Cloud (Prometheus, Loki, Tempo) |

---

## 1. GitHub Actions CI/CD Workflows

All workflows live in `.github/workflows/` and use standard kebab-case naming:

| Workflow File | Trigger | Responsibilities |
|---|---|---|
| `ci.yml` | Push to `master`, Pull Requests (app paths) | Runs FastAPI unit & integration tests (`uv run pytest`), Frontend unit & integration tests (`bun test`), strict TypeScript check (`tsc --noEmit`) and Vite production build (`bun run build`). Terraform checks are owned by the Terraform workflows. |
| `deploy-backend.yml` | Push to `master` on `apps/backend/**`, Manual dispatch | Authenticates to AWS via OIDC, builds the multi-stage `apps/backend/Dockerfile`, pushes `:latest` and `:<git-sha>` tags to Amazon ECR, triggers a rolling ECS Fargate deployment, waits for `services-stable`, and curls the ALB `/health` endpoint (the job fails if it never becomes healthy). |
| `deploy-frontend.yml` | Push to `master` on `apps/frontend/**`, Manual dispatch | Builds the SPA with `vercel build --prod` (so `VITE_BACKEND_API_URL` is baked in) and deploys the prebuilt output with `vercel deploy --prebuilt --prod`. |
| `terraform-aws.yml` | Push/PR to `infra/terraform/**`, Manual dispatch (`apply` defaults to false) | Chained jobs: `fmt` (no credentials) → `validate` (`init -backend=false`) → `plan` (Supabase S3 state, AWS OIDC, uploads `tfplan` artifact) → `apply` (push to `master` or dispatch with `apply`, `prod` environment, applies the exact plan artifact). |
| `terraform-supabase.yml` | Push/PR to `infra/terraform/supabase/**`, Manual dispatch (`apply` defaults to false) | Same `fmt` → `validate` → `plan` → `apply` chain for the Supabase PostgreSQL & Storage project configuration. |

The ML pipeline image (`pv-forecasting-pipeline`) is not built by CI; it is pushed manually. EventBridge schedule state comes from Terraform (`schedules_enabled`).

---

## 2. Container Packaging & ECS Task Definitions

The platform uses hardened multi-stage Docker images running as non-root `appuser` (`UID 10001`). In AWS ECS Fargate, five task definitions run inside private subnets:

| ECS Task Definition | Container Image | Command Override | CPU / Memory |
|---|---|---|---|
| `pv-forecasting-solar-api` | `pv-forecasting-backend:<sha>` | `uvicorn main:app --host 0.0.0.0 --port 8000` | `0.5 vCPU / 1 GB` |
| `pv-forecasting-mlflow-server` | `pv-forecasting-backend:<sha>` | `mlflow server --host 0.0.0.0 --port 5000 ...` | `0.5 vCPU / 1 GB` |
| `pv-forecasting-solar-ingest` | `pv-forecasting-pipeline:<sha>` | `python -m jobs.ingest` | `0.5 vCPU / 1 GB` |
| `pv-forecasting-solar-evaluate` | `pv-forecasting-pipeline:<sha>` | `python -m jobs.evaluate` | `0.5 vCPU / 1 GB` |
| `pv-forecasting-solar-drift` | `pv-forecasting-pipeline:<sha>` | `python -m jobs.drift_check` | `1.0 vCPU / 2 GB` |
| `pv-forecasting-solar-retrain` | `pv-forecasting-pipeline:<sha>` | `python -m jobs.retrain` | `2.0 vCPU / 4 GB` |

---

## 3. AWS Step Functions Orchestration

Rather than relying solely on fixed cron offsets that could run evaluation against incomplete data, `infra/terraform/modules/pipeline_scheduler/main.tf` provisions two AWS Step Functions state machines using synchronous ECS task execution (`arn:aws:states:::ecs:runTask.sync`):

1. **Daily MLOps State Machine (`pv-forecasting-daily-pipeline`):**
   ```text
   IngestAndPreprocess (Retry 2x, Catch -> SkipEvaluationOnIngestFailure)
     └──► Evaluate
            └──► Drift
                   └──► RetrainTriggerChoice (if LastTaskStatus == "DEGRADED" -> TriggerEarlyRetrain)
   ```
2. **Retraining State Machine (`pv-forecasting-retraining-pipeline`):**
   ```text
   RetrainChallenger (Retry 2x)
     └──► ValidateChallengerVsChampion
            └──► PromotionDecision (PromoteChallengerReady | RejectChallenger)
   ```

Failed scheduled invocations are routed to the SQS Dead-Letter Queue (`pv-forecasting-pipeline-dlq`, 14-day retention) so batch failures never pass silently.

---

## 4. Multi-Layer Monitoring & Alerting

### 4.1 Four Monitoring Layers

| Layer | Signals | Destination | Provisioned Dashboard |
|---|---|---|---|
| **Infrastructure** | ECS CPU utilization, memory utilization, running task count, ALB healthy hosts | CloudWatch Container Insights & Grafana | `pv_forecasting_infra_service.json` |
| **Service** | Request rate, HTTP 4xx/5xx error rate, p50/p95/p99 latency (`http_request_duration_seconds`) | Prometheus `/metrics` → Grafana Cloud | `pv_forecasting_infra_service.json` |
| **Data Quality** | Schema violations (`pv_ingestion_schema_violations`), QC flag counts per type (`pv_ingestion_range_flags{flag_type="missing|neg|over_kwp|stuck|day_zero"}`), rows per stage (`pv_ingestion_rows`), data lag (`pv_ingestion_data_lag_hours`) | `solar-ingest` → OTLP → Grafana Cloud | `pv_forecasting_overview.json` |
| **Model & Drift** | Live RMSE / MAE / R² and ratio to champion test RMSE (`pv_eval_*`), retrain decision (`pv_eval_needs_retrain`), KS drift share (`drift_score{feature="all"}`), per-feature KS (`pv_drift_feature_ks_stat{feature}`), target PSI (`pv_drift_target_psi`), weekly retrain (`pv_retrain_*`), job health (`pv_batch_job_duration_seconds` / `pv_batch_job_success{job_name}`), user OOD rate (`pv_user_ood_rate`) | Batch jobs → OTLP → Grafana Cloud; MLflow runs; API `/metrics` for OOD | `pv_forecasting_overview.json` |

### 4.1.1 Batch Job Metrics (OTLP)

The ingest, evaluate, drift and retrain jobs run as separate ECS tasks, so each one pushes its own results with `jobs/common/otel_metrics.py` right before it exits: one OTLP/HTTP (protobuf) export to `{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics`, authenticated with `OTEL_EXPORTER_OTLP_HEADERS` (URL-decoded, as in the API). `service.name` is the task name (`solar-ingest`, `solar-evaluate`, `solar-drift`, `solar-retrain`) and every point carries `service=<task name>`. Export is skipped when OTLP auth is missing, when `OTEL_SDK_DISABLED=true`, and on `--dry-run`. Export errors are logged and never change the job's exit code.

All job metrics are gauges with no unit, so Grafana Cloud keeps the names unchanged. Because each job reports once per run, dashboard queries wrap them in `last_over_time(...[26h])` (daily jobs) or `[8d]` (weekly retrain).

| Job | Metrics |
|---|---|
| all | `pv_batch_job_duration_seconds{job_name}`, `pv_batch_job_success{job_name}` (0/1) |
| `solar-evaluate` | `pv_eval_live_rmse`, `pv_eval_live_mae`, `pv_eval_live_r2`, `pv_eval_champion_test_rmse`, `pv_eval_rmse_ratio` (when a champion baseline exists), `pv_eval_needs_retrain` — all with `champion_version` |
| `solar-drift` | `drift_score{feature="all"}` (share of KS-drifted features), `pv_drift_feature_ks_stat{feature}`, `pv_drift_n_features_drifted`, `pv_drift_n_features_tested`, `pv_drift_target_psi{feature="target_norm"}`, `pv_drift_detected`; MLflow also gets `ks_stat_<feature>` |
| `solar-ingest` | `pv_ingestion_schema_violations`, `pv_ingestion_range_flags{flag_type}` (latest month), `pv_ingestion_rows{stage="train_table|usable|new"}`, `pv_ingestion_data_lag_hours`, `pv_ingestion_files_uploaded` |
| `solar-retrain` | `pv_retrain_test_rmse`, `pv_retrain_champion_test_rmse`, `pv_retrain_candidate_promoted` (0/1, `candidate` alias assigned) — with `model_version` |

### 4.2 Drift Taxonomy & Alert Matrix

| Condition | Category | Severity | Operational Action |
|---|---|---|---|
| Ingestion failure, schema violation, or night-irradiance violation | **Data Quality** | **Critical** | Reject batch, keep last valid snapshot, send immediate Discord alert |
| Reference-site nMAE above threshold for 3 consecutive days (or live RMSE ratio $> 1.15\times$) | **Concept Drift** | **Warning** | Trigger early `solar-retrain` run and notify team for Challenger review |
| Feature KS drift score $\ge 0.40$ or target PSI $> 0.20$ | **Covariate Drift** | **Info** | Log Markdown drift report to S3/MLflow for diagnosis; never triggers retrain alone |
| User request outside training range (`kwp` $\notin [1, 100]$ or `tilt` $\notin [0^\circ, 45^\circ]$) | **User Traffic OOD** | **Info** | Flag `is_ood = true` in `prediction_logs` and increment `pv_user_ood_rate` |

---

## 5. Verification & Rollback Procedures

### 5.1 Application & Model Rollback
- **Application Rollback (ECS Fargate):** Update `pv-forecasting-fastapi-service` to the previous ECS task definition revision or redeploy the prior Git commit SHA image from Amazon ECR:
  ```bash
  aws ecs update-service \
    --cluster pv-forecasting-cluster \
    --service pv-forecasting-fastapi-service \
    --task-definition pv-forecasting-solar-api:<PREVIOUS_REVISION> \
    --region ap-southeast-1
  ```
- **Model Rollback (MLflow Registry):** Re-assign the `@champion` alias to the previous validated model version via `promote.py`, followed by `POST /api/v1/model/reload`.
