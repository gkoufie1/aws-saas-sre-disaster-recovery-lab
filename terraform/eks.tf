module "eks" {
  #checkov:skip=CKV_TF_1:Registry module pinned by version constraint and .terraform.lock.hcl
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = local.name
  kubernetes_version = var.kubernetes_version

  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.admin_cidrs

  # The identity that runs `terraform apply` gets cluster-admin via an
  # EKS access entry (no aws-auth ConfigMap).
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Control-plane logs to CloudWatch: API + audit are the evidence trail
  # for "who changed what" during an incident review.
  enabled_log_types                      = ["api", "audit", "authenticator"]
  cloudwatch_log_group_retention_in_days = 7

  addons = {
    vpc-cni                = { before_compute = true }
    eks-pod-identity-agent = { before_compute = true }
    coredns                = {}
    kube-proxy             = {}
  }

  eks_managed_node_groups = {
    app = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = var.node_instance_types
      capacity_type  = var.node_capacity_type

      # Two nodes in two AZs so node- and AZ-loss drills have somewhere to go.
      min_size     = 2
      desired_size = 2
      max_size     = 3

      labels = { "lab.datareliability/pool" = "app" }

      metadata_options = {
        http_tokens                 = "required" # IMDSv2 only
        http_put_response_hop_limit = 1          # pods cannot reach node credentials
      }
    }
  }
}
