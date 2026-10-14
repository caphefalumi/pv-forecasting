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

- **Cloud Platform:** AWS (ap-south-1) for container compute, load balancing, networking, and data lake.
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
    │   ├── networking/        # VPC, 2 public & 2 private subnets, NAT, IGW
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

### Initialization:
Provide credentials via environment variables or a local `backend.hcl` (gitignored):
```bash
cd infra/terraform
terraform init \
  -backend-config="access_key=<SUPABASE_S3_ACCESS_KEY_ID>" \
  -backend-config="secret_key=<SUPABASE_S3_SECRET_ACCESS_KEY>"
```

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

The repository includes 5 GitHub Actions workflows:
1. `.github/workflows/ci.yml`: Unit & integration tests (`pytest` and `bun:test`), frontend build (`bun run build`), Ansible playbook check, and Terraform format/validation checks.
2. `.github/workflows/deploy-backend.yml`: Builds multi-stage Docker image, pushes to ECR, triggers rolling ECS deployment, and runs post-deployment smoke tests against the ALB DNS.
3. `.github/workflows/promote-model.yml`: Promotes a verified Challenger model version to the `@champion` alias in the MLflow Registry with approver provenance and Discord notification.
4. `.github/workflows/terraform-aws.yml`: Plans and applies AWS infrastructure using GitHub Actions OIDC and Supabase remote state.
5. `.github/workflows/terraform-supabase.yml`: Plans and applies Supabase project and API configurations.
