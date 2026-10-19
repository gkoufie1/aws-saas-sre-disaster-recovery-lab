variable "region" {
  description = "AWS region for the lab."
  type        = string
  default     = "us-east-2"
}

variable "environment" {
  description = "Environment name, used in resource names and tags."
  type        = string
  default     = "lab"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "kubernetes_version" {
  description = "EKS version. Semarchy's published self-hosted range is 1.32-1.35; the lab stays inside it."
  type        = string
  default     = "1.34"
}

variable "node_instance_types" {
  description = "Instance types for the app node group (Spot, so list several for capacity)."
  type        = list(string)
  default     = ["t3.medium", "t3a.medium"]
}

variable "node_capacity_type" {
  description = "SPOT for drills (cheaper, and interruptions are useful failure data) or ON_DEMAND."
  type        = string
  default     = "SPOT"
  validation {
    condition     = contains(["SPOT", "ON_DEMAND"], var.node_capacity_type)
    error_message = "node_capacity_type must be SPOT or ON_DEMAND."
  }
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_multi_az" {
  description = "Multi-AZ standby. Roughly doubles RDS cost; enable only for the AZ-failover drill."
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "Automated backup retention. Must be >= 1 for point-in-time recovery."
  type        = number
  default     = 3
  validation {
    condition     = var.db_backup_retention_days >= 1 && var.db_backup_retention_days <= 35
    error_message = "Retention must be 1-35 days; 0 disables backups and PITR."
  }
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. Set to your own IP /32."
  type        = list(string)
}

variable "budget_limit_usd" {
  description = "Monthly AWS Budget limit for this account."
  type        = number
  default     = 20
}

variable "budget_alert_email" {
  description = "Email notified at 50/80/100% of the budget."
  type        = string
}

variable "enable_guardduty" {
  description = "Enable GuardDuty (with EKS protection). Free for 30 days per account, billed after."
  type        = bool
  default     = false
}
