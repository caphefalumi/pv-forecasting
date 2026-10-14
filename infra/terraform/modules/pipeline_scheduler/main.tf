# ── Dead-Letter Queue for Scheduled Invocations (Section 4.8.9) ───────────────
resource "aws_sqs_queue" "pipeline_dlq" {
  name                      = "${var.project_name}-pipeline-dlq"
  message_retention_seconds = 1209600 # 14 days
}

# ── IAM Role for EventBridge Scheduler ───────────────────────────────────────
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
          "ecs:RunTask",
          "states:StartExecution"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.pipeline_dlq.arn
      },
      {
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [var.ecs_execution_role_arn, var.ecs_task_role_arn]
      }
    ]
  })
}

# ── IAM Role for AWS Step Functions (Section 4.8.5) ──────────────────────────
resource "aws_iam_role" "step_functions" {
  name = "${var.project_name}-step-functions-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_iam_role_policy" "step_functions" {
  name = "${var.project_name}-step-functions-policy"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecs:RunTask",
          "ecs:StopTask",
          "ecs:DescribeTasks",
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule"
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

# ── CloudWatch Log Group for Batch Pipeline Jobs (Section 4.8.9) ──────────────
resource "aws_cloudwatch_log_group" "pipeline_jobs" {
  name              = "/ecs/${var.project_name}-pipeline-jobs"
  retention_in_days = 30
}

locals {
  common_env = [
    { name = "PV_S3_URI", value = var.pv_s3_uri },
    { name = "SUPABASE_URL", value = var.supabase_url },
    { name = "SUPABASE_SERVICE_ROLE_KEY", value = var.supabase_service_role_key },
    { name = "SUPABASE_RAW_DATA_BUCKET", value = "processed_data" }
  ]

  ecs_network_config = {
    AwsvpcConfiguration = {
      Subnets        = var.private_subnet_ids
      SecurityGroups = [var.ecs_security_group_id]
      AssignPublicIp = "DISABLED"
    }
  }
}

# ── Shared Image, Multiple Task Definitions (Table 20, Section 4.8.4) ─────────

# 1. solar-ingest: python -m jobs.ingest (0.5 vCPU / 1 GB)
resource "aws_ecs_task_definition" "solar_ingest" {
  family                   = "${var.project_name}-solar-ingest"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name        = "solar-ingest"
      image       = "${var.pipeline_ecr_repository_url}:latest"
      essential   = true
      command     = ["python", "-m", "jobs.ingest"]
      environment = local.common_env
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline_jobs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "solar-ingest"
        }
      }
    }
  ])
}

# 2. solar-evaluate: python -m jobs.evaluate (0.5 vCPU / 1 GB)
resource "aws_ecs_task_definition" "solar_evaluate" {
  family                   = "${var.project_name}-solar-evaluate"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name        = "solar-evaluate"
      image       = "${var.pipeline_ecr_repository_url}:latest"
      essential   = true
      command     = ["python", "-m", "jobs.evaluate"]
      environment = local.common_env
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline_jobs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "solar-evaluate"
        }
      }
    }
  ])
}

# 3. solar-drift: python -m jobs.drift_check (1 vCPU / 2 GB)
resource "aws_ecs_task_definition" "solar_drift" {
  family                   = "${var.project_name}-solar-drift"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name        = "solar-drift"
      image       = "${var.pipeline_ecr_repository_url}:latest"
      essential   = true
      command     = ["python", "-m", "jobs.drift_check"]
      environment = local.common_env
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline_jobs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "solar-drift"
        }
      }
    }
  ])
}

# 4. solar-retrain: python -m jobs.retrain (2 vCPU / 4 GB)
resource "aws_ecs_task_definition" "solar_retrain" {
  family                   = "${var.project_name}-solar-retrain"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  container_definitions = jsonencode([
    {
      name        = "solar-retrain"
      image       = "${var.pipeline_ecr_repository_url}:latest"
      essential   = true
      command     = ["python", "-m", "jobs.retrain"]
      environment = local.common_env
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.pipeline_jobs.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "solar-retrain"
        }
      }
    }
  ])
}

# ── AWS Step Functions State Machines (Section 4.8.5 Orchestration) ───────────

# Daily State Machine: Ingest -> Evaluate -> Drift -> Choice
resource "aws_sfn_state_machine" "daily_pipeline" {
  name     = "${var.project_name}-daily-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "Daily MLOps Pipeline: Ingest -> Preprocess -> Evaluate -> Drift -> Choice (Section 4.8.5)"
    StartAt = "IngestAndPreprocess"
    States = {
      IngestAndPreprocess = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_ingest.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 30
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "SkipEvaluationOnIngestFailure"
          }
        ]
        Next = "Evaluate"
      }
      Evaluate = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_evaluate.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Next = "Drift"
      }
      Drift = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_drift.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Next = "RetrainTriggerChoice"
      }
      RetrainTriggerChoice = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.LastTaskStatus"
            StringMatches = "DEGRADED"
            Next          = "TriggerEarlyRetrain"
          }
        ]
        Default = "DailyPipelineSucceeded"
      }
      TriggerEarlyRetrain = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_retrain.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Next = "DailyPipelineSucceeded"
      }
      DailyPipelineSucceeded = {
        Type = "Succeed"
      }
      SkipEvaluationOnIngestFailure = {
        Type  = "Fail"
        Error = "IngestionFailed"
        Cause = "Persistent ingestion failure; skipping downstream evaluation against incomplete data."
      }
    }
  })
}

# Retraining State Machine: Retrain -> Validate (Challenger vs Champion) -> Promote / Reject
resource "aws_sfn_state_machine" "retraining_pipeline" {
  name     = "${var.project_name}-retraining-pipeline"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "Retraining Pipeline: Retrain -> Validate (Challenger vs Champion) -> Promote / Reject (Section 4.8.5)"
    StartAt = "RetrainChallenger"
    States = {
      RetrainChallenger = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_retrain.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 60
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Next = "ValidateChallengerVsChampion"
      }
      ValidateChallengerVsChampion = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType           = "FARGATE"
          Cluster              = var.cluster_arn
          TaskDefinition       = aws_ecs_task_definition.solar_evaluate.arn
          NetworkConfiguration = local.ecs_network_config
        }
        Next = "PromotionDecision"
      }
      PromotionDecision = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.ValidationStatus"
            StringMatches = "REJECTED"
            Next          = "RejectChallenger"
          }
        ]
        Default = "PromoteChallengerReady"
      }
      PromoteChallengerReady = {
        Type = "Succeed"
      }
      RejectChallenger = {
        Type  = "Fail"
        Error = "ChallengerRejected"
        Cause = "Challenger did not beat Champion nMAE margin or failed golden-set behavioural tests."
      }
    }
  })
}

# ── EventBridge Schedules (Table 15 & Section 4.8.9 with Retry + DLQ) ─────────

# 1. Ingest: Daily at 01:00 (Table 15)
resource "aws_scheduler_schedule" "daily_ingest" {
  name        = "${var.project_name}-daily-ingest"
  description = "Daily at 01:00: Collects new forecast and actual data into S3 (Table 15)"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 1 * * ? *)"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.eventbridge_ecs.arn

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }

    dead_letter_config {
      arn = aws_sqs_queue.pipeline_dlq.arn
    }

    input = jsonencode({
      Cluster              = var.cluster_arn
      TaskDefinition       = aws_ecs_task_definition.solar_ingest.arn
      LaunchType           = "FARGATE"
      NetworkConfiguration = local.ecs_network_config
    })
  }
}

# 2. Evaluate: Daily at 03:00 (Table 15)
resource "aws_scheduler_schedule" "daily_evaluate" {
  name        = "${var.project_name}-daily-evaluate"
  description = "Daily at 03:00: Computes nMAE of predictions vs. actual values (Table 15)"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 3 * * ? *)"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.eventbridge_ecs.arn

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }

    dead_letter_config {
      arn = aws_sqs_queue.pipeline_dlq.arn
    }

    input = jsonencode({
      Cluster              = var.cluster_arn
      TaskDefinition       = aws_ecs_task_definition.solar_evaluate.arn
      LaunchType           = "FARGATE"
      NetworkConfiguration = local.ecs_network_config
    })
  }
}

# 3. Drift: Daily at 04:00 (Table 15)
resource "aws_scheduler_schedule" "daily_drift" {
  name        = "${var.project_name}-daily-drift"
  description = "Daily at 04:00: Computes drift score and report with Evidently (Table 15)"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 4 * * ? *)"

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ecs:runTask"
    role_arn = aws_iam_role.eventbridge_ecs.arn

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }

    dead_letter_config {
      arn = aws_sqs_queue.pipeline_dlq.arn
    }

    input = jsonencode({
      Cluster              = var.cluster_arn
      TaskDefinition       = aws_ecs_task_definition.solar_drift.arn
      LaunchType           = "FARGATE"
      NetworkConfiguration = local.ecs_network_config
    })
  }
}

# 4. Retrain: Weekly (Table 15)
resource "aws_scheduler_schedule" "weekly_retrain" {
  name        = "${var.project_name}-weekly-retrain"
  description = "Weekly: Retrains the model and runs Challenger vs Champion validation (Table 15)"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression = "cron(0 2 ? * SUN *)"

  target {
    arn      = aws_sfn_state_machine.retraining_pipeline.arn
    role_arn = aws_iam_role.eventbridge_ecs.arn

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 7200
    }

    dead_letter_config {
      arn = aws_sqs_queue.pipeline_dlq.arn
    }

    input = jsonencode({
      TriggerSource = "weekly-schedule"
    })
  }
}
