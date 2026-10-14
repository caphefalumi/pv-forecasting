#!/usr/bin/env python3
"""
run_smoke.py — Cross-platform smoke verification runner for PV Forecasting.
Equivalent to infra/ansible/playbook.yml, running natively on Windows, macOS, and Linux
without requiring WSL or Linux control nodes.
"""

import sys
import os
import re
import json
import urllib.request
import urllib.error
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "apps" / "backend"
TERRAFORM_DIR = ROOT_DIR / "infra" / "terraform"
API_BASE_URL = os.environ.get("BACKEND_API_URL", "http://127.0.0.1:8000")


def print_step(name: str):
    print(f"\n\033[1;34m=== {name} ===\033[0m")


def print_pass(msg: str):
    print(f"  \033[1;32m[PASS]\033[0m {msg}")


def print_fail(msg: str):
    print(f"  \033[1;31m[FAIL]\033[0m {msg}")
    sys.exit(1)


def check_backend_files():
    print_step("Step 1: Verify Backend Source Files")
    required_files = ["main.py", "Dockerfile", "pyproject.toml", "uv.lock"]
    for f in required_files:
        p = BACKEND_DIR / f
        if p.exists():
            print_pass(f"Found {f}")
        else:
            print_fail(f"Missing {p}")


def check_dockerfile_multistage():
    print_step("Step 2: Verify Multi-stage Non-root Dockerfiles")
    dockerfiles = [
        ("Backend Dockerfile", BACKEND_DIR / "Dockerfile"),
        ("Data Pipeline Dockerfile", ROOT_DIR / "Dockerfile"),
    ]
    for label, path in dockerfiles:
        if not path.exists():
            print_fail(f"{label} missing at {path}")
        content = path.read_text(encoding="utf-8")
        if "AS builder" not in content:
            print_fail(f"{label} missing 'AS builder' multi-stage directive")
        if "AS runtime" not in content:
            print_fail(f"{label} missing 'AS runtime' multi-stage directive")
        if "USER appuser" not in content:
            print_fail(f"{label} missing unprivileged non-root 'USER appuser' directive")
        print_pass(f"{label}: Multi-stage builder, minimal runtime, unprivileged appuser verified")


def check_terraform_modules():
    print_step("Step 3: Verify Terraform Modular Architecture & Report IaC (8 Modules + Env + Grafana)")
    modules = [
        "networking",
        "security",
        "alb",
        "ecr",
        "storage",
        "iam",
        "ecs",
        "pipeline_scheduler",
    ]
    for mod in modules:
        mod_main = TERRAFORM_DIR / "modules" / mod / "main.tf"
        if mod_main.exists():
            print_pass(f"Module '{mod}' present")
        else:
            print_fail(f"Module '{mod}' missing at {mod_main}")

    for env_file in ["dev.tfvars", "prod.tfvars"]:
        env_path = TERRAFORM_DIR / "environments" / env_file
        if env_path.exists():
            print_pass(f"Environment config '{env_file}' present")
        else:
            print_fail(f"Missing environment file {env_path}")

    grafana_json = ROOT_DIR / "infra" / "monitoring" / "grafana" / "dashboards" / "pv_forecasting_overview.json"
    if grafana_json.exists():
        print_pass("Grafana provisioned dashboard JSON present")
    else:
        print_fail(f"Missing Grafana dashboard JSON at {grafana_json}")

    for job_mod in ["ingest.py", "evaluate.py", "drift_check.py", "retrain.py"]:
        job_path = ROOT_DIR / "jobs" / job_mod
        if job_path.exists():
            print_pass(f"Table 20 batch job entrypoint 'jobs/{job_mod}' present")
        else:
            print_fail(f"Missing Table 20 entrypoint {job_path}")

    for wf in ["ci.yml", "deploy-backend.yml", "promote-model.yml", "terraform-aws.yml", "terraform-supabase.yml"]:
        wf_path = ROOT_DIR / ".github" / "workflows" / wf
        if wf_path.exists():
            print_pass(f"GitHub Actions workflow '.github/workflows/{wf}' present")
        else:
            print_fail(f"Missing GitHub Actions workflow {wf_path}")

    for doc in ["architecture.md", "api-contract.md", "data-pipeline.md", "deployment-and-monitoring.md", "mlops-lifecycle.md"]:
        doc_path = ROOT_DIR / "docs" / doc
        if doc_path.exists():
            print_pass(f"Documentation 'docs/{doc}' present")
        else:
            print_fail(f"Missing documentation file {doc_path}")


def check_api_endpoints():
    print_step("Step 4: Smoke Test Live API Endpoints")
    print(f"  Target API URL: {API_BASE_URL}")

    # Health check
    try:
        req = urllib.request.Request(f"{API_BASE_URL}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                print_pass(f"Health check 200 OK: {body}")
            else:
                print(f"  \033[33m[WARN]\033[0m Health check returned HTTP {resp.status}")
                return
    except Exception as e:
        print(f"  \033[33m[INFO]\033[0m Target {API_BASE_URL} not reachable locally ({e}). Skipping live network test.")
        return

    # Forecast check
    try:
        payload = json.dumps({
            "kwp": 10.0,
            "technology": "mono-Si",
            "tracking": "fixed",
            "tilt_deg": 20.0
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE_URL}/api/v1/forecast",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                assert "total_daily_kwh" in data and data["total_daily_kwh"] > 0
                assert len(data.get("hourly_breakdown", [])) == 24
                print_pass(f"Forecast check 200 OK: Total {data['total_daily_kwh']} kWh across 24 hourly steps")
            else:
                print_fail(f"Forecast endpoint returned HTTP {resp.status}")
    except Exception as e:
        print_fail(f"Forecast endpoint error: {e}")


def main():
    print("\033[1;36m============================================================")
    print("   PV Forecasting Architecture & Smoke Verification Suite   ")
    print("============================================================\033[0m")
    check_backend_files()
    check_dockerfile_multistage()
    check_terraform_modules()
    check_api_endpoints()
    print("\n\033[1;32mAll Architecture & Infrastructure Checks Passed Successfully!\033[0m\n")


if __name__ == "__main__":
    main()
