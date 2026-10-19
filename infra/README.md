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
- **Database & State:** Supabase for PostgreSQL prediction storage (`prediction_logs`) and S3-compatible object storage (`terraform-state`).
- **Containers:** Multi-stage Docker packaging (`builder` + non-root `appuser` runtime).
- **Automation:** EventBridge cron triggering daily data ingestion and model evaluation.

---

## 2. Directory Structure

```
infra/
├── ansible/
│   ├── ansible.cfg            # Ansible execution configuration
│   ├── inventory.ini          # Inventory definition (local, staging, prod)
│   ├── playbook.yml           # Verification playbook (Docker, modules, endpoints)
│   └── run_smoke.py           # Cross-platform runner (Windows / macOS / Linux)
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
    │   ├── storage/           # S3 data lake bucket with SSE-S3 and versioning
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

## 4. Running Verification & Smoke Tests

### Option A: Cross-Platform Python Runner (Windows / macOS / Linux)
```bash
python infra/ansible/run_smoke.py
```
To run against a live running backend or deployed ALB:
```bash
BACKEND_API_URL="http://pv-forecasting-alb-12345.ap-south-1.elb.amazonaws.com" python infra/ansible/run_smoke.py
```

### Option B: Ansible Playbook (Linux / macOS / WSL)
```bash
ansible-playbook -i infra/ansible/inventory.ini infra/ansible/playbook.yml \
  -e "api_base_url=http://127.0.0.1:8000"
```

---

## 5. CI/CD Workflows

1. `.github/workflows/ci.yml` — tests + Terraform/Ansible checks
2. `.github/workflows/deploy-backend.yml` — FastAPI image → ECR → ECS
3. `.github/workflows/deploy-pipeline.yml` — root image (`ingest.py` / `evaluate.py` / `drift.py` / `retrain.py`) → pipeline ECR
4. `.github/workflows/deploy-frontend.yml` — Vercel production deploy
5. `.github/workflows/demo-mode.yml` — **start** (API count=1 + enable schedules) / **stop** (count=0 + disable schedules)
6. `.github/workflows/promote-model.yml` — MLflow Champion promotion
7. `.github/workflows/terraform-aws.yml` / `terraform-supabase.yml` — IaC

Default idle: FastAPI `desired_count=0`, EventBridge schedules `DISABLED`. Use **Demo Mode → start** before a presentation.
