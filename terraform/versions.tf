terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Remote state. Bootstrap the bucket once, then:
  #   terraform init -backend-config="bucket=<state-bucket>" -backend-config="region=us-east-1"
  backend "s3" {
    key          = "datareliability/lab/terraform.tfstate"
    use_lockfile = true # S3-native state locking, no DynamoDB table needed
    encrypt      = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "datareliability"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Everything in this stack is meant to be short-lived; see README cost section.
      Lifecycle = "ephemeral-lab"
    }
  }
}
