output "lambda_function_name" {
  description = "Name of the monitor Lambda function"
  value       = aws_lambda_function.monitor.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.monitor.arn
}

output "alerts_topic_arn" {
  description = "SNS topic that receives replication-slot alerts and monitor-failure alarms. Subscribe a PagerDuty integration (or anything else) to this."
  value       = aws_sns_topic.alerts.arn
}

output "schedule_name" {
  value = aws_scheduler_schedule.monitor.name
}
