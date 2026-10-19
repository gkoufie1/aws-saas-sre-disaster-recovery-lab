# Amazon RDS for PostgreSQL 16, the managed equivalent of the local lab's
# StatefulSet. 16 also matches Semarchy's documented minimum (16.4+).

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL from EKS nodes only"
  vpc_id      = module.vpc.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_nodes" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = module.eks.node_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "PostgreSQL from EKS worker nodes"
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "log_min_duration_statement"
    value = "250" # same slow-query threshold as the local lab
  }
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

resource "aws_db_instance" "postgres" {
  #checkov:skip=CKV_AWS_157:Multi-AZ is a variable (db_multi_az), enabled only for the AZ-failover drill
  #checkov:skip=CKV_AWS_293:Deletion protection off so terraform destroy returns the account to $0; production sets it on
  #checkov:skip=CKV_AWS_118:Enhanced monitoring adds an IAM role and per-second metrics cost; CloudWatch basic + postgres logs used
  #checkov:skip=CKV_AWS_353:Performance Insights not used on db.t4g.micro in this lab; slow queries come from log_min_duration_statement
  identifier     = "${local.name}-postgres"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 50
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "customers"
  username = "app"
  # RDS generates the password and stores/rotates it in Secrets Manager;
  # it never appears in Terraform state or variables.
  manage_master_user_password         = true
  iam_database_authentication_enabled = true

  db_subnet_group_name   = module.vpc.database_subnet_group_name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  # Automated backups + transaction logs = point-in-time recovery, which is
  # what makes a ~5-minute RPO achievable (unlike 5-minute pg_dump files).
  backup_retention_period = var.db_backup_retention_days
  backup_window           = "07:00-07:30"
  maintenance_window      = "sun:08:00-sun:08:30"
  copy_tags_to_snapshot   = true

  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true

  # Lab settings: no final snapshot and no deletion protection, so
  # `terraform destroy` reliably returns the account to $0.
  # Production: deletion_protection = true, skip_final_snapshot = false.
  deletion_protection = false
  skip_final_snapshot = true
  apply_immediately   = true
}
