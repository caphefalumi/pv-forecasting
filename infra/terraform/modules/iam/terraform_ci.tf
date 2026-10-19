# Terraform-in-CI permissions for the GitHub Actions role (terraform-aws.yml plan/apply).
# Split in two customer-managed policies (inline policies share a 10 KB per-role limit):
#   * terraform-read  - everything `terraform plan` needs to refresh state (read/Describe/List/Get)
#   * terraform-write - the create/update/delete actions `terraform apply` needs
# Scoped to this account, the deploy region and "${var.project_name}-*" names wherever the
# service supports resource-level permissions; Describe/List APIs that do not are left on "*".
# The DenySelfEscalation statement blocks CI from editing its own role or these two policies;
# apply changes to them locally (terraform apply -target=...) with admin credentials.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  prefix     = var.project_name
  arn_ctx    = "${local.region}:${local.account_id}"
}

resource "aws_iam_policy" "terraform_read" {
  name        = "${var.project_name}-terraform-read"
  description = "Read-only access Terraform plan needs for the ${var.project_name} AWS stack"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DescribeListUnscoped"
        Effect = "Allow"
        Action = [
          "sts:GetCallerIdentity",
          "acm:ListCertificates",
          "ec2:DescribeVpcs",
          "ec2:DescribeVpcAttribute",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeSecurityGroupRules",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeAccountAttributes",
          "ec2:DescribeAvailabilityZones",
          "ecr:DescribeRepositories",
          "ecs:DescribeClusters",
          "ecs:DescribeServices",
          "ecs:DescribeTaskDefinition",
          "ecs:ListClusters",
          "ecs:ListServices",
          "ecs:ListTaskDefinitions",
          "ecs:ListTagsForResource",
          "elasticloadbalancing:Describe*",
          "logs:DescribeLogGroups",
          "states:ListStateMachines",
          "scheduler:ListSchedules",
          "sqs:ListQueues",
          "iam:ListOpenIDConnectProviders"
        ]
        Resource = "*"
      },
      {
        Sid    = "AcmRead"
        Effect = "Allow"
        Action = [
          "acm:DescribeCertificate",
          "acm:GetCertificate",
          "acm:ListTagsForCertificate"
        ]
        Resource = "arn:aws:acm:${local.arn_ctx}:certificate/*"
      },
      {
        Sid    = "EcrRead"
        Effect = "Allow"
        Action = [
          "ecr:GetLifecyclePolicy",
          "ecr:GetRepositoryPolicy",
          "ecr:ListTagsForResource",
          "ecr:DescribeImages"
        ]
        Resource = "arn:aws:ecr:${local.arn_ctx}:repository/${local.prefix}-*"
      },
      {
        Sid    = "LogsRead"
        Effect = "Allow"
        Action = [
          "logs:ListTagsForResource",
          "logs:ListTagsLogGroup"
        ]
        Resource = "arn:aws:logs:${local.arn_ctx}:log-group:/ecs/${local.prefix}-*"
      },
      {
        Sid    = "IamRead"
        Effect = "Allow"
        Action = [
          "iam:GetRole",
          "iam:GetRolePolicy",
          "iam:ListRolePolicies",
          "iam:ListAttachedRolePolicies",
          "iam:ListRoleTags",
          "iam:ListInstanceProfilesForRole",
          "iam:GetPolicy",
          "iam:GetPolicyVersion",
          "iam:ListPolicyVersions",
          "iam:ListPolicyTags",
          "iam:GetOpenIDConnectProvider",
          "iam:ListOpenIDConnectProviderTags"
        ]
        Resource = [
          "arn:aws:iam::${local.account_id}:role/${local.prefix}-*",
          "arn:aws:iam::${local.account_id}:policy/${local.prefix}-*",
          "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
          "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
        ]
      },
      {
        Sid    = "SqsRead"
        Effect = "Allow"
        Action = [
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl",
          "sqs:ListQueueTags"
        ]
        Resource = "arn:aws:sqs:${local.arn_ctx}:${local.prefix}-*"
      },
      {
        Sid    = "StatesRead"
        Effect = "Allow"
        Action = [
          "states:DescribeStateMachine",
          "states:ListStateMachineVersions",
          "states:ListStateMachineAliases",
          "states:ListTagsForResource"
        ]
        Resource = "arn:aws:states:${local.arn_ctx}:stateMachine:${local.prefix}-*"
      },
      {
        Sid    = "SchedulerRead"
        Effect = "Allow"
        Action = [
          "scheduler:GetSchedule",
          "scheduler:ListTagsForResource"
        ]
        Resource = "arn:aws:scheduler:${local.arn_ctx}:schedule/default/${local.prefix}-*"
      }
    ]
  })
}

resource "aws_iam_policy" "terraform_write" {
  name        = "${var.project_name}-terraform-write"
  description = "Create/update/delete access Terraform apply needs for the ${var.project_name} AWS stack"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Stop CI from escalating its own access: it may not change its own role's
        # policies or trust, or rewrite the two Terraform CI policies. Changes to these
        # resources are applied locally by an admin.
        Sid    = "DenySelfEscalation"
        Effect = "Deny"
        Action = [
          "iam:Put*",
          "iam:Attach*",
          "iam:Detach*",
          "iam:DeleteRolePolicy",
          "iam:DeleteRolePermissionsBoundary",
          "iam:UpdateAssumeRolePolicy",
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:SetDefaultPolicyVersion",
          "iam:DeletePolicy"
        ]
        Resource = [
          aws_iam_role.github_actions.arn,
          "arn:aws:iam::${local.account_id}:policy/${local.prefix}-terraform-read",
          "arn:aws:iam::${local.account_id}:policy/${local.prefix}-terraform-write"
        ]
      },
      {
        Sid      = "AcmRequest"
        Effect   = "Allow"
        Action   = ["acm:RequestCertificate"]
        Resource = "*"
      },
      {
        Sid    = "AcmManage"
        Effect = "Allow"
        Action = [
          "acm:DeleteCertificate",
          "acm:AddTagsToCertificate",
          "acm:RemoveTagsFromCertificate"
        ]
        Resource = "arn:aws:acm:${local.arn_ctx}:certificate/*"
      },
      {
        Sid    = "SecurityGroups"
        Effect = "Allow"
        Action = [
          "ec2:CreateSecurityGroup",
          "ec2:DeleteSecurityGroup",
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:AuthorizeSecurityGroupEgress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupEgress",
          "ec2:ModifySecurityGroupRules",
          "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
          "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
          "ec2:CreateTags",
          "ec2:DeleteTags"
        ]
        Resource = [
          "arn:aws:ec2:${local.arn_ctx}:security-group/*",
          "arn:aws:ec2:${local.arn_ctx}:security-group-rule/*",
          "arn:aws:ec2:${local.arn_ctx}:vpc/*"
        ]
      },
      {
        Sid    = "EcrManage"
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository",
          "ecr:DeleteRepository",
          "ecr:PutLifecyclePolicy",
          "ecr:DeleteLifecyclePolicy",
          "ecr:PutImageTagMutability",
          "ecr:PutImageScanningConfiguration",
          "ecr:TagResource",
          "ecr:UntagResource"
        ]
        Resource = "arn:aws:ecr:${local.arn_ctx}:repository/${local.prefix}-*"
      },
      {
        Sid    = "EcsManage"
        Effect = "Allow"
        Action = [
          "ecs:CreateCluster",
          "ecs:DeleteCluster",
          "ecs:UpdateCluster",
          "ecs:UpdateClusterSettings",
          "ecs:PutClusterCapacityProviders",
          "ecs:CreateService",
          "ecs:UpdateService",
          "ecs:DeleteService",
          "ecs:TagResource",
          "ecs:UntagResource"
        ]
        Resource = [
          "arn:aws:ecs:${local.arn_ctx}:cluster/${local.prefix}-*",
          "arn:aws:ecs:${local.arn_ctx}:service/${local.prefix}-*/*",
          "arn:aws:ecs:${local.arn_ctx}:task-definition/${local.prefix}-*:*"
        ]
      },
      {
        # Register/Deregister do not support resource-level permissions.
        Sid      = "EcsTaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition"]
        Resource = "*"
      },
      {
        Sid    = "ElbManage"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:ModifyLoadBalancerAttributes",
          "elasticloadbalancing:SetSecurityGroups",
          "elasticloadbalancing:SetSubnets",
          "elasticloadbalancing:CreateTargetGroup",
          "elasticloadbalancing:DeleteTargetGroup",
          "elasticloadbalancing:ModifyTargetGroup",
          "elasticloadbalancing:ModifyTargetGroupAttributes",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:ModifyListenerAttributes",
          "elasticloadbalancing:AddListenerCertificates",
          "elasticloadbalancing:RemoveListenerCertificates",
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:${local.arn_ctx}:loadbalancer/app/${local.prefix}-*/*",
          "arn:aws:elasticloadbalancing:${local.arn_ctx}:targetgroup/${local.prefix}-*/*",
          "arn:aws:elasticloadbalancing:${local.arn_ctx}:listener/app/${local.prefix}-*/*/*"
        ]
      },
      {
        Sid    = "LogsManage"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:DeleteLogGroup",
          "logs:PutRetentionPolicy",
          "logs:DeleteRetentionPolicy",
          "logs:TagResource",
          "logs:UntagResource",
          "logs:TagLogGroup",
          "logs:UntagLogGroup"
        ]
        Resource = "arn:aws:logs:${local.arn_ctx}:log-group:/ecs/${local.prefix}-*"
      },
      {
        Sid    = "IamRolesManage"
        Effect = "Allow"
        Action = [
          "iam:CreateRole",
          "iam:DeleteRole",
          "iam:UpdateRole",
          "iam:UpdateAssumeRolePolicy",
          "iam:PutRolePolicy",
          "iam:DeleteRolePolicy",
          "iam:TagRole",
          "iam:UntagRole"
        ]
        Resource = "arn:aws:iam::${local.account_id}:role/${local.prefix}-*"
      },
      {
        # Only project policies and the ECS execution managed policy may be attached.
        Sid      = "IamAttachProjectPolicies"
        Effect   = "Allow"
        Action   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource = "arn:aws:iam::${local.account_id}:role/${local.prefix}-*"
        Condition = {
          ArnLike = {
            "iam:PolicyARN" = [
              "arn:aws:iam::${local.account_id}:policy/${local.prefix}-*",
              "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            ]
          }
        }
      },
      {
        Sid    = "IamPoliciesManage"
        Effect = "Allow"
        Action = [
          "iam:CreatePolicy",
          "iam:DeletePolicy",
          "iam:CreatePolicyVersion",
          "iam:DeletePolicyVersion",
          "iam:TagPolicy",
          "iam:UntagPolicy"
        ]
        Resource = "arn:aws:iam::${local.account_id}:policy/${local.prefix}-*"
      },
      {
        Sid      = "IamPassProjectRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:aws:iam::${local.account_id}:role/${local.prefix}-*"
        Condition = {
          StringEquals = {
            "iam:PassedToService" = [
              "ecs-tasks.amazonaws.com",
              "states.amazonaws.com",
              "scheduler.amazonaws.com"
            ]
          }
        }
      },
      {
        Sid    = "GithubOidcProvider"
        Effect = "Allow"
        Action = [
          "iam:CreateOpenIDConnectProvider",
          "iam:DeleteOpenIDConnectProvider",
          "iam:UpdateOpenIDConnectProviderThumbprint",
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:RemoveClientIDFromOpenIDConnectProvider",
          "iam:TagOpenIDConnectProvider",
          "iam:UntagOpenIDConnectProvider"
        ]
        Resource = "arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"
      },
      {
        Sid    = "SqsManage"
        Effect = "Allow"
        Action = [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:SetQueueAttributes",
          "sqs:TagQueue",
          "sqs:UntagQueue"
        ]
        Resource = "arn:aws:sqs:${local.arn_ctx}:${local.prefix}-*"
      },
      {
        Sid    = "StatesManage"
        Effect = "Allow"
        Action = [
          "states:CreateStateMachine",
          "states:UpdateStateMachine",
          "states:DeleteStateMachine",
          "states:TagResource",
          "states:UntagResource"
        ]
        Resource = "arn:aws:states:${local.arn_ctx}:stateMachine:${local.prefix}-*"
      },
      {
        Sid    = "SchedulerManage"
        Effect = "Allow"
        Action = [
          "scheduler:CreateSchedule",
          "scheduler:UpdateSchedule",
          "scheduler:DeleteSchedule",
          "scheduler:TagResource",
          "scheduler:UntagResource"
        ]
        Resource = "arn:aws:scheduler:${local.arn_ctx}:schedule/default/${local.prefix}-*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "github_actions_terraform_read" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.terraform_read.arn
}

resource "aws_iam_role_policy_attachment" "github_actions_terraform_write" {
  role       = aws_iam_role.github_actions.name
  policy_arn = aws_iam_policy.terraform_write.arn
}
