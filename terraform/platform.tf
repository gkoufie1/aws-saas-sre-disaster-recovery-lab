# Container registry, cost guardrail and threat detection.

resource "aws_ecr_repository" "images" {
  for_each = toset(["datareliability-api", "datareliability-loadgen", "datareliability-backup"])

  #checkov:skip=CKV_AWS_136:AES256 (AWS-managed) encryption; customer-managed KMS not required for public base images + lab code
  name                 = each.key
  image_tag_mutability = "IMMUTABLE" # a tag always means the same image; rollbacks are trustworthy
  force_delete         = true        # lab only

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "images" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# Cost is a reliability signal too: an account-level budget with alerts
# at 50/80/100% of actual spend and 100% of forecast.
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

resource "aws_guardduty_detector" "this" {
  #checkov:skip=CKV2_AWS_3:Single-account lab; no AWS Organizations delegated admin to configure
  count  = var.enable_guardduty ? 1 : 0
  enable = true
}

resource "aws_guardduty_detector_feature" "eks_audit" {
  count       = var.enable_guardduty ? 1 : 0
  detector_id = aws_guardduty_detector.this[0].id
  name        = "EKS_AUDIT_LOGS"
  status      = "ENABLED"
}
