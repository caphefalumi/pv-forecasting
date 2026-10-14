resource "aws_iam_role" "eventbridge_ecs" {
  name = "${var.project_name}-eventbridge-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "scheduler.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "eventbridge_ecs" {
  name = "${var.project_name}-eventbridge-policy"
  role = aws_iam_role.eventbridge_ecs.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [var.ecs_execution_role_arn, var.ecs_task_role_arn]
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "pipeline_jobs" {
  name              = "/ecs/${var.project_name}-pipeline-jobs"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "ingest_job" {
  family                   = "${var.project_name}-ingest-job"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "ingest"
      image     = "${var.pipeline_ecr_repository_url}:latest"
      essential = true
      command   = ["python", "-m", "pvdata", "run"]

      environment = [
        { name = "PV_S3_URI", value = var.pv_s3_uri },
        { name = "SUPABASE_URL", value = var.supabase_url },
        { name = "SUPABASE_SERVICE_ROLE_KEY", value = var.supabase_service_role_key },
        { name = "SUPABASE_RAW_DATA_BUCKET", value = "processed_data" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline_jobs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "ingest"
        }
      }
    }
  ])
}

resource "aws_scheduler_schedule" "daily_ingest" {
  name        = "${var.project_name}-daily-ingest"
  description = "Triggers Ingest + Eval + Drift scheduled pipeline daily"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 0 * * ? *)"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.eventbridge_ecs.arn

    input = jsonencode({
      Cluster        = var.cluster_arn
      TaskDefinition = aws_ecs_task_definition.ingest_job.arn
      LaunchType     = "FARGATE"
      NetworkConfiguration = {
        AwsvpcConfiguration = {
          Subnets        = var.private_subnet_ids
          SecurityGroups = [var.ecs_security_group_id]
          AssignPublicIp = "DISABLED"
        }
      }
    })
  }
}
