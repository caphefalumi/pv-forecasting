output "daily_schedule_arn" {
  description = "ARN of the daily Ingest EventBridge schedule (01:00)"
  value       = aws_scheduler_schedule.daily_ingest.arn
}

output "daily_evaluate_schedule_arn" {
  description = "ARN of the daily Evaluate EventBridge schedule (03:00)"
  value       = aws_scheduler_schedule.daily_evaluate.arn
}

output "daily_drift_schedule_arn" {
  description = "ARN of the daily Drift EventBridge schedule (04:00)"
  value       = aws_scheduler_schedule.daily_drift.arn
}

output "weekly_retrain_schedule_arn" {
  description = "ARN of the weekly Retrain EventBridge schedule"
  value       = aws_scheduler_schedule.weekly_retrain.arn
}

output "daily_pipeline_state_machine_arn" {
  description = "ARN of the Daily MLOps Step Functions state machine"
  value       = aws_sfn_state_machine.daily_pipeline.arn
}

output "retraining_pipeline_state_machine_arn" {
  description = "ARN of the Retraining Step Functions state machine"
  value       = aws_sfn_state_machine.retraining_pipeline.arn
}

output "pipeline_dlq_arn" {
  description = "ARN of the SQS Dead-Letter Queue for failed scheduled runs"
  value       = aws_sqs_queue.pipeline_dlq.arn
}
