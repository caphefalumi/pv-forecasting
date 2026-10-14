output "bucket_name" {
  description = "Name of the S3 data lake bucket"
  value       = aws_s3_bucket.data_lake.id
}

output "bucket_arn" {
  description = "ARN of the S3 data lake bucket"
  value       = aws_s3_bucket.data_lake.arn
}

output "bucket_uri" {
  description = "S3 URI format for synchronization"
  value       = "s3://${aws_s3_bucket.data_lake.id}"
}
