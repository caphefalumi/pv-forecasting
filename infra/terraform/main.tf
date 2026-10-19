terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "Terraform"
    }
  }
}

# ── Module 1: Networking (VPC, Subnets, NAT, IGW) ──────────────────────────────
module "networking" {
  source       = "./modules/networking"
  project_name = var.project_name
}

# ── Module 2: Security Groups (ALB & ECS Fargate) ─────────────────────────────
module "security" {
  source       = "./modules/security"
  project_name = var.project_name
  vpc_id       = module.networking.vpc_id
}

# ── Module 3: Application Load Balancer ───────────────────────────────────────
module "alb" {
  source                = "./modules/alb"
  project_name          = var.project_name
  vpc_id                = module.networking.vpc_id
  public_subnet_ids     = module.networking.public_subnet_ids
  alb_security_group_id = module.security.alb_security_group_id
}

# ── Module 4: Amazon ECR ──────────────────────────────────────────────────────
module "ecr" {
  source       = "./modules/ecr"
  project_name = var.project_name
}

# ── Module 5: S3 Data Lake & Model Storage ────────────────────────────────────
module "storage" {
  source       = "./modules/storage"
  project_name = var.project_name
}

# ── Module 6: IAM Roles (GitHub OIDC & ECS) ───────────────────────────────────
module "iam" {
  source                  = "./modules/iam"
  project_name            = var.project_name
  github_repo             = var.github_repo
  s3_data_lake_bucket_arn = module.storage.bucket_arn
}

# ── Module 7: ECS Fargate Cluster & Services (FastAPI & MLflow) ───────────────
module "ecs" {
  source                    = "./modules/ecs"
  project_name              = var.project_name
  aws_region                = var.aws_region
  private_subnet_ids        = module.networking.private_subnet_ids
  ecs_security_group_id     = module.security.ecs_security_group_id
  target_group_arn          = module.alb.target_group_arn
  ecr_repository_url        = module.ecr.backend_repository_url
  ecs_execution_role_arn    = module.iam.ecs_execution_role_arn
  ecs_task_role_arn         = module.iam.ecs_task_role_arn
  supabase_url                 = var.supabase_url
  supabase_service_role_key    = var.supabase_service_role_key
  mlflow_tracking_uri          = var.mlflow_tracking_uri
  s3_bucket_uri                = module.storage.bucket_uri
  otel_exporter_otlp_protocol  = var.otel_exporter_otlp_protocol
  otel_exporter_otlp_endpoint  = var.otel_exporter_otlp_endpoint
  otel_exporter_otlp_headers   = var.otel_exporter_otlp_headers
  grafana_otlp_instance_id     = var.grafana_otlp_instance_id
  grafana_cloud_api_token      = var.grafana_cloud_api_token
  api_desired_count            = var.api_desired_count
}

# ── Module 8: EventBridge Pipeline Scheduler ───────────────────────────────────
module "pipeline_scheduler" {
  source                      = "./modules/pipeline_scheduler"
  project_name                = var.project_name
  aws_region                  = var.aws_region
  cluster_arn                 = module.ecs.cluster_arn
  private_subnet_ids          = module.networking.private_subnet_ids
  ecs_security_group_id       = module.security.ecs_security_group_id
  pipeline_ecr_repository_url = module.ecr.pipeline_repository_url
  ecs_execution_role_arn      = module.iam.ecs_execution_role_arn
  ecs_task_role_arn           = module.iam.ecs_task_role_arn
  supabase_url                 = var.supabase_url
  supabase_service_role_key    = var.supabase_service_role_key
  pv_s3_uri                    = module.storage.bucket_uri
  otel_exporter_otlp_protocol  = var.otel_exporter_otlp_protocol
  otel_exporter_otlp_endpoint  = var.otel_exporter_otlp_endpoint
  otel_exporter_otlp_headers   = var.otel_exporter_otlp_headers
  grafana_otlp_instance_id     = var.grafana_otlp_instance_id
  grafana_cloud_api_token      = var.grafana_cloud_api_token
  schedules_enabled            = var.schedules_enabled
}
