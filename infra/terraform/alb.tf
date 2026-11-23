# HTTPS for api.<zone>: the certificate, its DNS records in Cloudflare, and the load balancer (ADR-030 §4).

locals {
  api_domain = "${var.api_subdomain}.${var.zone}"
}

data "cloudflare_zone" "this" {
  filter = {
    name = var.zone
  }
}

resource "aws_acm_certificate" "api" {
  domain_name       = local.api_domain
  validation_method = "DNS"

  # A replacement certificate is issued before the old one is removed, so HTTPS never goes down.
  lifecycle {
    create_before_destroy = true
  }
}

# The record that proves to AWS we own the domain. DNS only: Cloudflare must not proxy it.
resource "cloudflare_dns_record" "cert_validation" {
  for_each = {
    for o in aws_acm_certificate.api.domain_validation_options : o.domain_name => o
  }

  zone_id = data.cloudflare_zone.this.zone_id
  name    = trimsuffix(each.value.resource_record_name, ".")
  type    = each.value.resource_record_type
  content = trimsuffix(each.value.resource_record_value, ".")
  ttl     = 60
  proxied = false
}

# Waits until AWS has seen the record and issued the certificate.
resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for o in aws_acm_certificate.api.domain_validation_options : o.resource_record_name]

  depends_on = [cloudflare_dns_record.cert_validation]
}

resource "aws_lb" "api" {
  name               = "agentspace-api"
  load_balancer_type = "application"
  internal           = false
  subnets            = data.aws_subnets.default.ids
  security_groups    = [aws_security_group.alb.id]
  # Well above the stream's 15 s ping (ADR-028), so a quiet stream isn't cut.
  idle_timeout = 120
}

resource "aws_lb_target_group" "api" {
  name        = "agentspace-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = data.aws_vpc.default.id
  # An old task keeps its open streams this long after a deploy deregisters it (ADR-030 §3).
  deregistration_delay = 120

  health_check {
    path                = "/health"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  stickiness {
    type    = "lb_cookie"
    enabled = false
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

# api.<zone> → the load balancer. DNS only: Cloudflare's proxy adds its own timeouts and can cut long replies.
resource "cloudflare_dns_record" "api" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = local.api_domain
  type    = "CNAME"
  content = aws_lb.api.dns_name
  ttl     = 300
  proxied = false
}
