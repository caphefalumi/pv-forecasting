resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ── 1. FastAPI Prediction Service (Table 20: solar-api, 0.5 vCPU / 1 GB) ─────
# Grafana Cloud credentials live in SSM Parameter Store (SecureString, AWS-managed aws/ssm
# key) instead of plain task-definition environment. Shared with the pipeline task definitions.
resource "aws_ssm_parameter" "otel_exporter_otlp_headers" {
  name        = "${var.project_name}-otel-exporter-otlp-headers"
  description = "OTEL_EXPORTER_OTLP_HEADERS for Grafana Cloud OTLP"
  type        = "SecureString"
  value       = var.otel_exporter_otlp_headers
}

resource "aws_ssm_parameter" "grafana_cloud_api_token" {
  name        = "${var.project_name}-grafana-cloud-api-token"
  description = "GRAFANA_CLOUD_API_TOKEN for Grafana Cloud"
  type        = "SecureString"
  value       = var.grafana_cloud_api_token
}

locals {
  grafana_secrets = [
    { name = "OTEL_EXPORTER_OTLP_HEADERS", valueFrom = aws_ssm_parameter.otel_exporter_otlp_headers.arn },
    { name = "GRAFANA_CLOUD_API_TOKEN", valueFrom = aws_ssm_parameter.grafana_cloud_api_token.arn }
  ]
}

resource "aws_cloudwatch_log_group" "fastapi" {
  name              = "/ecs/${var.project_name}-solar-api"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "fastapi" {
  family                   = "${var.project_name}-solar-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "fastapi-service"
      image     = "${var.ecr_repository_url}:latest"
      essential = true
      command   = ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "HOST", value = "0.0.0.0" },
        { name = "PORT", value = "8000" },
        { name = "CORS_ORIGINS", value = jsonencode(var.cors_origins) },
        { name = "SUPABASE_URL", value = var.supabase_url },
        { name = "SUPABASE_SERVICE_ROLE_KEY", value = var.supabase_service_role_key },
        { name = "SUPABASE_RAW_DATA_BUCKET", value = var.supabase_storage_bucket },
        { name = "MLFLOW_TRACKING_URI", value = var.mlflow_tracking_uri },
        { name = "MLFLOW_S3_ENDPOINT_URL", value = var.supabase_s3_endpoint },
        { name = "AWS_ACCESS_KEY_ID", value = var.supabase_s3_access_key },
        { name = "AWS_SECRET_ACCESS_KEY", value = var.supabase_s3_secret_key },
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
        { name = "AWS_ENDPOINT_URL_S3", value = var.supabase_s3_endpoint },
        { name = "OTEL_EXPORTER_OTLP_PROTOCOL", value = var.otel_exporter_otlp_protocol },
        { name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = var.otel_exporter_otlp_endpoint },
        { name = "GRAFANA_OTLP_INSTANCE_ID", value = var.grafana_otlp_instance_id }
      ]

      # Injected by the ECS agent at task start (execution role reads the SSM parameters).
      secrets = local.grafana_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.fastapi.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "solar-api"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 15
      }
    }
  ])
}

resource "aws_ecs_service" "fastapi" {
  name            = "${var.project_name}-fastapi-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.fastapi.arn
  desired_count   = var.api_desired_count # 0 idle; scale to 1 for demos via aws ecs update-service
  launch_type     = "FARGATE"

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = true # default VPC public subnets; no NAT Gateway
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "fastapi-service"
    container_port   = 8000
  }

  # Runtime count is changed manually (0 idle / 1 demo); TF only sets the initial value.
  lifecycle {
    ignore_changes = [desired_count]
  }
}

# ── 2. MLflow Tracking & Registry Server on ECS Fargate (Section 4.4.2 & 4.6) ─
resource "aws_cloudwatch_log_group" "mlflow" {
  name              = "/ecs/${var.project_name}-mlflow-server"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "mlflow" {
  family                   = "${var.project_name}-mlflow-server"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "mlflow-server"
      image     = "${var.mlflow_ecr_repository_url}:latest"
      essential = true
      command = [
        "mlflow",
        "server",
        "--host",
        "0.0.0.0",
        "--port",
        "5000",
        # MLflow 3.x defaults to 4 uvicorn workers, which OOM-kills the 1 GB task.
        "--workers",
        "1",
        "--default-artifact-root",
        var.mlflow_artifact_root
      ]

      portMappings = [
        {
          containerPort = 5000
          hostPort      = 5000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "MLFLOW_BACKEND_STORE_URI", value = var.mlflow_tracking_uri },
        { name = "MLFLOW_S3_ENDPOINT_URL", value = var.supabase_s3_endpoint },
        { name = "AWS_ACCESS_KEY_ID", value = var.supabase_s3_access_key },
        { name = "AWS_SECRET_ACCESS_KEY", value = var.supabase_s3_secret_key },
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
        { name = "AWS_ENDPOINT_URL_S3", value = var.supabase_s3_endpoint }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.mlflow.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "mlflow"
        }
      }

      # The image has no curl; MLflow serves GET /health -> "OK".
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=4)\" || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
    }
  ])
}

resource "aws_ecs_service" "mlflow" {
  name            = "${var.project_name}-mlflow-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mlflow.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = true # default VPC public subnets; no NAT Gateway
  }

  # Runtime count is changed manually (0 idle / 1 when needed); TF only sets the initial value.
  lifecycle {
    ignore_changes = [desired_count]
  }
}
