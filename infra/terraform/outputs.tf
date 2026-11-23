# What `terraform apply` prints at the end.

output "api_url" {
  value = "https://${local.api_domain}"
}

output "load_balancer_dns" {
  value = aws_lb.api.dns_name
}

output "ecr_repository" {
  value = aws_ecr_repository.api.repository_url
}
