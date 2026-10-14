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
        { name = "SUPABASE_URL", value = var.supabase_url },
        { name = "SUPABASE_SERVICE_ROLE_KEY", value = var.supabase_service_role_key },
        { name = "SUPABASE_RAW_DATA_BUCKET", value = "processed_data" },
        { name = "MLFLOW_TRACKING_URI", value = var.mlflow_tracking_uri },
        { name = "GRAFANA_PROMETHEUS_URL", value = "https://prometheus-prod-37-prod-ap-southeast-1.grafana.net/api/prom" },
        { name = "GRAFANA_PROMETHEUS_USER", value = "3616746" },
        { name = "GRAFANA_LOKI_URL", value = "https://logs-prod-020.grafana.net/loki/api/v1/push" },
        { name = "GRAFANA_LOKI_USER", value = "1804041" },
        { name = "GRAFANA_TEMPO_URL", value = "https://tempo-prod-14-prod-ap-southeast-1.grafana.net/tempo" },
        { name = "GRAFANA_TEMPO_USER", value = "1798340" },
        { name = "GRAFANA_OTLP_ENDPOINT", value = "https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp" }
      ]

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
  desired_count   = 1
  launch_type     = "FARGATE"

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.ecs_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "fastapi-service"
    container_port   = 8000
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
      image     = "${var.ecr_repository_url}:latest"
      essential = true
      command = [
        "mlflow",
        "server",
        "--host",
        "0.0.0.0",
        "--port",
        "5000",
        "--default-artifact-root",
        "${var.s3_bucket_uri}/artifacts"
      ]

      portMappings = [
        {
          containerPort = 5000
          hostPort      = 5000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "MLFLOW_BACKEND_STORE_URI", value = var.mlflow_tracking_uri }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.mlflow.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "mlflow"
        }
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
    assign_public_ip = false
  }
}
