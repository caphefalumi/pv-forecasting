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

- **Cloud Platform:** AWS (ap-south-1) for container compute, load balancing, and data lake. Networking uses the **account default VPC** (no custom VPC / IGW / NAT) with ECS `assign_public_ip = true` to cut cost.
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

Or use GitHub Actions → **Terraform AWS** → Run workflow (`workflow_dispatch`) with Apply checked.
Requires `environment: prod` secrets: `SUPABASE_S3_*`, `AWS_ROLE_ARN`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `MLFLOW_TRACKING_URI`, OTEL secrets.

---

## 4. CI/CD Workflows

1. `.github/workflows/ci.yml` — tests + Terraform fmt/validate
2. `.github/workflows/deploy-backend.yml` — FastAPI image → ECR → ECS (+ `/health` check)
3. `.github/workflows/deploy-pipeline.yml` — root image (`ingest.py` / `evaluate.py` / `drift.py` / `retrain.py`) → pipeline ECR
4. `.github/workflows/deploy-frontend.yml` — Vercel production deploy
5. `.github/workflows/demo-mode.yml` — **start** / **stop** demo scaling
6. `.github/workflows/promote-model.yml` — MLflow Champion promotion
7. `.github/workflows/terraform-aws.yml` / `terraform-supabase.yml` — IaC

Default idle: FastAPI `desired_count=0`, EventBridge schedules `DISABLED`. Use **Demo Mode → start** before a presentation.
