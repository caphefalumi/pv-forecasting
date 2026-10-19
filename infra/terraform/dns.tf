# Cloudflare DNS + ACM for api-pv.tm-project.id.vn → ALB (HTTPS)
#
# Auth MUST be API Token (Bearer). If CLOUDFLARE_API_KEY is also in the
# environment, the provider falls into legacy Global-Key mode and errors with
# "Missing X-Auth-Email header". Unset CLOUDFLARE_API_KEY before terraform,
# and pass the token via TF_VAR_cloudflare_api_token / CLOUDFLARE_API_TOKEN.

provider "cloudflare" {
  api_token = trimspace(var.cloudflare_api_token)
}

# Zone ID is provided via var (default set); no live zone lookup needed.
locals {
  cloudflare_zone_id = var.cloudflare_zone_id
}

# CAA 0 issue "amazon.com" is managed outside Terraform (Cloudflare API) —
# required for ACM; provider v5 CAA data schema is awkward to update in-place.

resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${var.project_name}-api-cert"
  }
}

resource "cloudflare_dns_record" "acm_validation" {
  for_each = {
    for dvo in aws_acm_certificate.api.domain_validation_options : dvo.domain_name => {
      name  = dvo.resource_record_name
      type  = dvo.resource_record_type
      value = dvo.resource_record_value
    }
  }

  zone_id = local.cloudflare_zone_id
  name    = each.value.name
  type    = each.value.type
  content = trimsuffix(each.value.value, ".")
  ttl     = 60
  proxied = false
  comment = "ACM DNS validation for ${var.api_domain}"
}

resource "aws_acm_certificate_validation" "api" {
  certificate_arn = aws_acm_certificate.api.arn
  validation_record_fqdns = [
    for dvo in aws_acm_certificate.api.domain_validation_options : dvo.resource_record_name
  ]
  depends_on = [cloudflare_dns_record.acm_validation]
}

# Orange-cloud CNAME: visitors hit Cloudflare; origin is the ALB over HTTPS (Full strict).
resource "cloudflare_dns_record" "api" {
  zone_id = local.cloudflare_zone_id
  name    = var.api_domain
  type    = "CNAME"
  content = module.alb.alb_dns_name
  ttl     = 1 # auto when proxied
  proxied = true
  comment = "PV Forecasting FastAPI ALB"

  depends_on = [aws_acm_certificate_validation.api]
}

resource "cloudflare_zone_setting" "ssl" {
  zone_id    = local.cloudflare_zone_id
  setting_id = "ssl"
  value      = "strict"
}
