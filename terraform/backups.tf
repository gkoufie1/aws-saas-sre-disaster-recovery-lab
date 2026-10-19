# Logical backups (pg_dump) land here, same tooling as the local lab.
# They complement, not replace, RDS automated backups: a logical dump is
# portable (another region, another engine version, a laptop) and is what
# you restore from if the RDS instance *and* its snapshots are unavailable.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backups" {
  #checkov:skip=CKV_AWS_18:Access logging needs a second bucket; accepted for an ephemeral lab (see docs/security/iac-scan.md)
  #checkov:skip=CKV_AWS_144:Cross-region replication is the next DR step, not in scope for Milestone 2
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) chosen over a customer-managed KMS key ($1/month per key)
  #checkov:skip=CKV2_AWS_62:No consumer for bucket event notifications; backup freshness is alerted from Prometheus
  bucket        = "${local.name}-backups-${data.aws_caller_identity.current.account_id}"
  force_destroy = true # lab only: lets destroy remove non-empty bucket
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled" # an overwritten or deleted backup is still recoverable
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    id     = "expire-old-dumps"
    status = "Enabled"
    filter {
      prefix = "postgres/"
    }
    expiration {
      days = 7
    }
    noncurrent_version_expiration {
      noncurrent_days = 3
    }
  }

  # pg_dump streams to S3 as a multipart upload; a killed job leaves parts
  # behind that are billed but invisible in a normal listing.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "backups_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.backups.arn,
      "${aws_s3_bucket.backups.arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = data.aws_iam_policy_document.backups_tls_only.json
}

# ---------------------------------------------------------------------------
# EKS Pod Identity: only the backup ServiceAccount can write dumps, and it
# cannot delete them (a compromised backup job cannot wipe history).
data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${local.name}-backup"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

data "aws_iam_policy_document" "backup_access" {
  statement {
    sid       = "ListBackups"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backups.arn]
  }
  statement {
    sid       = "ReadWriteDumps"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.backups.arn}/postgres/*"]
  }
}

resource "aws_iam_role_policy" "backup" {
  name   = "backup-s3"
  role   = aws_iam_role.backup.id
  policy = data.aws_iam_policy_document.backup_access.json
}

resource "aws_eks_pod_identity_association" "backup" {
  cluster_name    = module.eks.cluster_name
  namespace       = "datareliability"
  service_account = "postgres-backup"
  role_arn        = aws_iam_role.backup.arn
}
