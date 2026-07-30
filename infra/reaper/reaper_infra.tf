# reaper_infra.tf
# PERMANENT infrastructure. Deployed from a SEPARATE Terraform state
# (backend key: "permanent/reaper/terraform.tfstate") from the ephemeral
# demo stack it watches. Tagged Environment=Permanent -- must never carry
# Environment=Demo, or the reaper could delete itself during a sweep.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "s3" {
    bucket         = "gauravdev-cloudops-tfstate"   # created in Phase 1
    key            = "permanent/reaper/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "gauravdev-cloudops-tf-locks"  # created in Phase 1
    encrypt        = true
  }
}

provider "aws" {
  region = "ap-south-1"
  default_tags {
    tags = {
      Project     = "aws-3tier-cloudops-platform"
      Environment = "Permanent"
      ManagedBy   = "terraform-reaper-stack"
    }
  }
}

data "archive_file" "reaper_zip" {
  type        = "zip"
  source_file = "${path.module}/reaper_lambda.py"
  output_path = "${path.module}/build/reaper_lambda.zip"
}

resource "aws_iam_role" "reaper_role" {
  name = "demo-reaper-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "reaper_policy" {
  name = "demo-reaper-permissions"
  role = aws_iam_role.reaper_role.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "TaggingRead"
        Effect = "Allow"
        Action = ["tag:GetResources"]
        Resource = "*"
      },
      {
        Sid    = "TeardownActions"
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:Describe*",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:DeleteLoadBalancer",
          "elasticloadbalancing:DeleteTargetGroup",
          "ecs:ListServices",
          "ecs:UpdateService",
          "ecs:DeleteService",
          "ecs:DescribeServices",
          "autoscaling:UpdateAutoScalingGroup",
          "autoscaling:DescribeAutoScalingGroups",
          "rds:DescribeDBInstances",
          "rds:DeleteDBInstance",
          "ec2:DescribeVpcEndpoints",
          "ec2:DeleteVpcEndpoints",
          "wafv2:GetWebACL",
          "wafv2:DeleteWebACL",
          "wafv2:DisassociateWebACL",
        ]
        Resource = "*"
      },
      {
        Sid      = "Logging"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid      = "Metrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_lambda_function" "reaper" {
  function_name    = "demo-cost-reaper"
  role             = aws_iam_role.reaper_role.arn
  handler          = "reaper_lambda.handler"
  runtime          = "python3.12"
  timeout          = 120
  filename         = data.archive_file.reaper_zip.output_path
  source_code_hash = data.archive_file.reaper_zip.output_base64sha256

  environment {
    variables = {
      SESSION_MAX_HOURS = "2"
    }
  }
}

resource "aws_cloudwatch_event_rule" "hourly" {
  name                = "demo-reaper-hourly"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "invoke_reaper" {
  rule      = aws_cloudwatch_event_rule.hourly.name
  target_id = "reaper-lambda"
  arn       = aws_lambda_function.reaper.arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.hourly.arn
}
