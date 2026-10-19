output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name} --alias ${module.eks.cluster_name}"
}

output "db_endpoint" {
  value = aws_db_instance.postgres.address
}

output "db_identifier" {
  description = "Used by the PITR drill: aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier <this>"
  value       = aws_db_instance.postgres.identifier
}

output "db_master_secret_arn" {
  description = "Secrets Manager secret holding the RDS-managed master password."
  value       = aws_db_instance.postgres.master_user_secret[0].secret_arn
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.images : k => r.repository_url }
}
