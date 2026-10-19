data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "datareliability-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  #checkov:skip=CKV_TF_1:Registry module pinned by version constraint and .terraform.lock.hcl; commit-hash sources lose registry semantics
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = local.name
  cidr = var.vpc_cidr
  azs  = local.azs

  private_subnets  = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets   = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 100 + i)]
  database_subnets = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 8, 110 + i)]

  # One NAT gateway, not one per AZ: a deliberate cost trade-off for an
  # ephemeral lab. It is a single point of failure for egress, recorded in
  # docs/adr/003-single-nat-gateway.md. Production would use one per AZ.
  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  create_database_subnet_group = true
  enable_dns_hostnames         = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}
