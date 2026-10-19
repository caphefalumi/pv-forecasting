resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "Allows incoming HTTP/HTTPS traffic to Application Load Balancer"
  vpc_id      = var.vpc_id

  # Origin lock-down: the API is only reachable through the proxied Cloudflare
  # record, so ingress is limited to Cloudflare's published edge ranges.
  ingress {
    description      = "HTTP from Cloudflare edge only"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = var.alb_ingress_ipv4_cidrs
    ipv6_cidr_blocks = var.alb_ingress_ipv6_cidrs
  }

  ingress {
    description      = "HTTPS from Cloudflare edge only"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = var.alb_ingress_ipv4_cidrs
    ipv6_cidr_blocks = var.alb_ingress_ipv6_cidrs
  }

  egress {
    description = "Outbound to ECS tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-alb-sg"
  }
}

resource "aws_security_group" "ecs" {
  name        = "${var.project_name}-ecs-sg"
  description = "Controls inbound traffic to ECS Fargate tasks from ALB only"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Inbound port 8000 from ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    description = "Outbound traffic for Open-Meteo, Supabase, MLflow via NAT"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-ecs-sg"
  }
}
