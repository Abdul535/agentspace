# Terraform and provider versions, and where the state lives (ADR-031).

terraform {
  # 1.10+ locks the S3 state with a lock file, so no DynamoDB table is needed.
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }

  # The bucket name holds the account id, so it's passed by `bootstrap.sh` (`terraform init -backend-config=...`).
  backend "s3" {
    key          = "agentspace/prod.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  # Tags every resource, so the bill and the console show what belongs to agentspace.
  default_tags {
    tags = {
      app       = "agentspace"
      env       = "prod"
      managedBy = "terraform"
    }
  }
}

# Reads its token from the CLOUDFLARE_API_TOKEN environment variable.
provider "cloudflare" {}
