# Infrastructure & Automation Guide — COS40007 PV Forecasting

This directory contains the Infrastructure as Code (Terraform) and Smoke Automation (Ansible + Python) for the COS40007 PV Forecasting System.

---

## 1. Architecture Overview

```
                      [ Vercel Client (Frontend) ]
                                   │
                                   ▼ HTTP (Port 80 / 443)
              ┌─────────────────────────────────────────┐
              │ AWS Application Load Balancer (Public)  │
              └────────────────────┬────────────────────┘
                                   │ Forward to Port 8000
                                   ▼
              ┌─────────────────────────────────────────┐
              │ AWS ECS Fargate: FastAPI Service (Priv) │
              └───────────────┬─────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
  [ Open-Meteo API ]  [ Supabase DB & S3 ]  [ MLflow Registry ]
  (Weather Forecast)   (Logs & State Lake)  (Champion Alias)
```

- **Cloud Platform:** AWS (ap-southeast-1) for container compute, load balancing, and data lake. Networking uses the **account default VPC** (no custom VPC / IGW / NAT) with ECS `assign_public_ip = true` to cut cost.
- **Database & Object storage:** Supabase PostgreSQL + Supabase S3-compatible Storage for **everything** (Terraform state, datasets, MLflow artifacts). No AWS S3 data lake.
- **Containers:** Multi-stage Docker packaging (`builder` + non-root `appuser` runtime).
- **Automation:** EventBridge cron triggering daily data ingestion and model evaluation.

---

## 2. Directory Structure

```
infra/
├── monitoring/                # Grafana dashboard JSON (version-controlled)
└── terraform/
    ├── backend.tf             # Remote S3 state backend pointing to Supabase Storage
    ├── main.tf                # Root module composition
    ├── variables.tf           # Environment parameters
    ├── outputs.tf             # Exposed endpoints (ALB DNS, ECR URLs, IAM roles)
    ├── modules/
    │   ├── networking/        # Default VPC lookup only (no custom VPC/NAT)
    │   ├── security/          # ALB SG (80/443) and ECS SG (port 8000 from ALB)
    │   ├── alb/               # Application Load Balancer & /health Target Group
    │   ├── ecr/               # Backend and pipeline ECR repositories with lifecycles
    │   ├── iam/               # GitHub Actions OIDC role & ECS task execution roles
    │   ├── ecs/               # ECS Fargate cluster, task definition, and service
    │   └── pipeline_scheduler/# EventBridge daily cron and ingestion task definition
    └── supabase/
        ├── provider.tf        # Supabase provider with remote state
        ├── resource.tf        # Supabase project settings
        └── settings.tf        # Database API schema and search path configuration
```

---

## 3. Remote State Backend (Supabase Storage)

Both Terraform stacks store state files remotely in the Supabase `terraform-state` storage bucket using S3-compatible endpoints without requiring DynamoDB locks.

### State Keys:
* AWS Infrastructure: `terraform-state/aws/terraform.tfstate`
* Supabase Stack: `terraform-state/supabase/terraform.tfstate`

### Initialization (always from `infra/terraform`, never repo root):
```bash
cd infra/terraform
terraform init -backend-config=backend.hcl   # gitignored; Supabase S3 keys
terraform validate
terraform plan
terraform apply
```

Or use GitHub Actions → **Terraform AWS** → Run workflow (`workflow_dispatch`) on `master` with **apply** checked (it defaults to plan-only).
Requires `environment: prod` secrets: `SUPABASE_S3_*`, `AWS_ROLE_ARN`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `MLFLOW_TRACKING_URI`, OTEL secrets.

---

## 4. CI/CD Workflows

1. `.github/workflows/ci.yml` — backend + frontend tests, typecheck and build
2. `.github/workflows/deploy-backend.yml` — FastAPI image → ECR → ECS (+ `/health` check, fails the job if unhealthy)
3. `.github/workflows/deploy-frontend.yml` — Vercel prebuilt production deploy
4. `.github/workflows/terraform-aws.yml` / `terraform-supabase.yml` — IaC, chained `fmt` → `validate` → `plan` → `apply`. PRs touching `infra/terraform/**` run fmt, validate and plan; apply only runs on push to `master` (or a manual dispatch with **apply** checked) in the `prod` environment and applies the exact plan artifact.

Terraform fmt/validate are owned by the Terraform workflows only (not `ci.yml`).

The ML pipeline image (`pv-forecasting-pipeline` ECR repo, used by the scheduled ingest/evaluate/drift/retrain tasks) is **not** built by CI; build and push it manually when the ML code changes.

Default idle: FastAPI `desired_count=0`, EventBridge schedules `DISABLED` (controlled by the Terraform variables `api_desired_count` and `schedules_enabled`). Before a presentation, scale up manually:

```bash
aws ecs update-service --cluster pv-forecasting-cluster --service pv-forecasting-fastapi-service --desired-count 1 --region ap-southeast-1
```

and set `schedules_enabled = true` in Terraform if the scheduled jobs should run.

GitHub `prod` environment secret required by **Terraform Supabase**: `SUPABASE_DB_PASSWORD` (Supabase database master password, at least 4 characters; mapped to `TF_VAR_database_password`). Create it with:

```bash
gh secret set SUPABASE_DB_PASSWORD --env prod --repo COS40007-2026-MinhHA10-Fall26/pv-forecasting
```

The plan and apply jobs fail early with a clear error if it is missing.

AWS OIDC: the repo uses GitHub's immutable OIDC subject, so the IAM trust matches `repo:COS40007-2026-MinhHA10-Fall26@330655750/pv-forecasting@1398967244:environment:prod` and `...:ref:refs/heads/master` (Terraform variable `github_oidc_sub_prefix`). Every AWS job must run in the `prod` environment or on `master` without an environment.

GitHub `prod` environment variables used by the workflows (set manually, no longer written by CI): `VITE_BACKEND_API_URL`, `API_DOMAIN`, `FRONTEND_ORIGIN`, `CLOUDFLARE_ZONE_NAME`, `CLOUDFLARE_ZONE_ID`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `GRAFANA_OTLP_INSTANCE_ID`.
