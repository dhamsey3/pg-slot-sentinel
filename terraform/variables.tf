variable "project_name" {
  description = "Prefix used for all resource names"
  type        = string
  default     = "pg-slot-sentinel"
}

variable "lambda_zip_path" {
  description = "Path to the packaged Lambda deployment zip, produced by scripts/build_lambda.sh"
  type        = string
  default     = "../build/monitor.zip"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression for how often to run the check"
  type        = string
  default     = "rate(15 minutes)"
}

variable "wal_retention_alert_bytes" {
  description = "Alert when a replication slot is retaining at least this many bytes of WAL"
  type        = number
  default     = 5368709120 # 5 GiB
}

variable "db_host" {
  description = "RDS endpoint hostname"
  type        = string
}

variable "db_port" {
  description = "RDS port"
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "Database to connect to and query pg_replication_slots from"
  type        = string
}

variable "db_user" {
  description = "Read-only database user; needs no privileges beyond the default ones required to query pg_replication_slots"
  type        = string
}

variable "db_password_secret_arn" {
  description = "Secrets Manager secret ARN holding {\"password\": \"...\"} for db_user"
  type        = string
}

variable "notification_email" {
  description = "Email address subscribed to the alert SNS topic. Leave empty to manage subscriptions (e.g. a PagerDuty integration) outside Terraform."
  type        = string
  default     = ""
}

variable "vpc_subnet_ids" {
  description = "Subnet IDs the Lambda uses to reach the RDS instance. Required if the database is not publicly reachable, which it should not be."
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  description = "Security group IDs to attach to the Lambda's ENIs, granting it access to the database's security group"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource this module creates"
  type        = map(string)
  default     = {}
}
