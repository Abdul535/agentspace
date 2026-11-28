# Inputs. The defaults are production's values; none of them is a secret.

variable "region" {
  description = "AWS region: the same as Upstash Redis and Supabase Postgres (Singapore), so each call stays close."
  type        = string
  default     = "ap-southeast-1"
}

variable "zone" {
  description = "The Cloudflare DNS zone (the domain)."
  type        = string
  default     = "hackerlab.network"
}

variable "api_subdomain" {
  description = "The API answers at <api_subdomain>.<zone>."
  type        = string
  default     = "api"
}

variable "supabase_url" {
  description = "The Supabase project whose sign-in tokens the API accepts (AGENTSPACE_SUPABASE_URL)."
  type        = string
  default     = "https://pahaqzlwyfalonosbxxd.supabase.co"
}

variable "sandboxes_enabled" {
  description = "Run each session's tools in its own Daytona sandbox (ADR-035): makes the Daytona key secret and turns the backend on."
  type        = bool
  default     = false
}

variable "langfuse_enabled" {
  description = "Make the Langfuse key secrets and pass them to the API (tracing, ADR-018)."
  type        = bool
  default     = false
}
