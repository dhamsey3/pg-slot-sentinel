terraform {
  required_version = ">= 1.5.7"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  in_vpc = length(var.vpc_subnet_ids) > 0
}

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_iam_role" "lambda" {
  name = "${var.project_name}-monitor"
  tags = var.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${var.project_name}-monitor"
  role = aws_iam_role.lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [
        {
          Sid      = "Logs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:*"
        },
        {
          Sid      = "ReadDbPassword"
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = var.db_password_secret_arn
        },
        {
          Sid      = "PublishMetrics"
          Effect   = "Allow"
          Action   = ["cloudwatch:PutMetricData"]
          Resource = "*"
        },
        {
          Sid      = "PublishAlerts"
          Effect   = "Allow"
          Action   = ["sns:Publish"]
          Resource = aws_sns_topic.alerts.arn
        },
      ],
      local.in_vpc ? [
        {
          Sid    = "ManageNetworkInterfaces"
          Effect = "Allow"
          Action = [
            "ec2:CreateNetworkInterface",
            "ec2:DescribeNetworkInterfaces",
            "ec2:DeleteNetworkInterface",
          ]
          Resource = "*"
        },
      ] : []
    )
  })
}

resource "aws_lambda_function" "monitor" {
  function_name    = "${var.project_name}-monitor"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  tags             = var.tags

  environment {
    variables = {
      DB_HOST                   = var.db_host
      DB_PORT                   = tostring(var.db_port)
      DB_NAME                   = var.db_name
      DB_USER                   = var.db_user
      DB_PASSWORD_SECRET_ARN    = var.db_password_secret_arn
      WAL_RETENTION_ALERT_BYTES = tostring(var.wal_retention_alert_bytes)
      SNS_TOPIC_ARN             = aws_sns_topic.alerts.arn
      METRIC_NAMESPACE          = var.project_name
      DRY_RUN                   = "false"
    }
  }

  dynamic "vpc_config" {
    for_each = local.in_vpc ? [1] : []
    content {
      subnet_ids         = var.vpc_subnet_ids
      security_group_ids = var.vpc_security_group_ids
    }
  }
}

resource "aws_cloudwatch_log_group" "monitor" {
  name              = "/aws/lambda/${aws_lambda_function.monitor.function_name}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_iam_role" "scheduler" {
  name = "${var.project_name}-scheduler"
  tags = var.tags

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.project_name}-scheduler"
  role = aws_iam_role.scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.monitor.arn
    }]
  })
}

resource "aws_scheduler_schedule" "monitor" {
  name                         = "${var.project_name}-schedule"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.monitor.arn
    role_arn = aws_iam_role.scheduler.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "monitor_errors" {
  alarm_name          = "${var.project_name}-monitor-errors"
  alarm_description   = "The pg-slot-sentinel monitor Lambda itself is failing to run - this is not a replication-slot alarm, it means the check isn't happening at all"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.monitor.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  tags                = var.tags
}

# Deliberately no CloudWatch alarm on ReplicationSlotRetainedBytes itself:
# the Lambda publishes that metric per-slot with a SlotName dimension, and
# CloudWatch alarms cannot wildcard across dimension values - an alarm
# without a fixed SlotName would just never receive data and silently
# never fire. The per-slot metric exists for graphing/dashboards; the
# actual alert on "some slot crossed the threshold" is sent directly by
# the Lambda to SNS in monitor/handler.py, which has no such limitation.
