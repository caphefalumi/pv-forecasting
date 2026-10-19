# Grafana Cloud reads CloudWatch (ECS CPU/memory, ALB, SQS ...) by assuming this role from
# Grafana's AWS account. The external ID is this Grafana Cloud stack's ID; keep the condition,
# it is what stops other Grafana customers from assuming the role.
# Actions: Grafana Cloud "Configure CloudWatch metrics scrape" minimum list plus the
# CloudWatch data source metrics permissions (all read-only).
resource "aws_iam_role" "grafana_cloudwatch" {
  name        = "${var.project_name}-grafana-cloudwatch"
  description = "Assumed by Grafana Cloud to read CloudWatch metrics"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.grafana_aws_account_id}:root" }
        Action    = "sts:AssumeRole"
        Condition = {
          StringEquals = { "sts:ExternalId" = var.grafana_external_id }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "grafana_cloudwatch" {
  name = "${var.project_name}-grafana-cloudwatch-read"
  role = aws_iam_role.grafana_cloudwatch.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CloudWatchRead"
        Effect = "Allow"
        Action = [
          "tag:GetResources",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:DescribeAlarms",
          "cloudwatch:DescribeAlarmsForMetric",
          "cloudwatch:DescribeAlarmHistory",
          "cloudwatch:GetInsightRuleReport",
          "ec2:DescribeRegions",
          "ec2:DescribeTags",
          "ec2:DescribeInstances",
          "ec2:DescribeTransitGatewayAttachments",
          "ec2:DescribeSpotFleetRequests",
          "iam:ListAccountAliases",
          "apigateway:GET",
          "aps:ListWorkspaces",
          "autoscaling:DescribeAutoScalingGroups",
          "dms:DescribeReplicationInstances",
          "dms:DescribeReplicationTasks",
          "shield:ListProtections",
          "storagegateway:ListGateways",
          "storagegateway:ListTagsForResource"
        ]
        Resource = "*"
      }
    ]
  })
}