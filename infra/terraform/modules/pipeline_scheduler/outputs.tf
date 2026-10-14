output "daily_schedule_arn" {
  description = "ARN of the daily EventBridge schedule"
  value       = aws_scheduler_schedule.daily_ingest.arn
}
