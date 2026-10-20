# Hệ Thống Giám Sát và Cảnh Báo Grafana Cloud MLOps

Tài liệu hướng dẫn và đặc tả cấu hình giám sát, cảnh báo (Alerting Rules) và bảng điều khiển (Dashboards) được chuẩn hoá dưới dạng Declarative Provisioning cho nền tảng dự báo điện mặt trời **PV Forecasting Platform**.

---

## 1. Kiến Trúc Giám Sát 4 Lớp (Observability Architecture)

Hệ thống giám sát vận hành dựa trên Grafana Cloud (Prometheus, Loki, Tempo), CloudWatch Container Insights và OpenTelemetry OTLP:

| Lớp Giám Sát | Nguồn Dữ Liệu & Giao Thức | Chỉ Số Chính (Metrics) | Bảng Điều Khiển (Dashboard) |
|---|---|---|---|
| **1. Hạ Tầng (Infrastructure)** | AWS CloudWatch Container Insights | ECS CPU/Memory Utilization theo Service, Network I/O, Active Tasks | `pv_forecasting_infra_service.json` |
| **2. Dịch Vụ API (Service)** | FastAPI `solar-api` qua Prometheus `/metrics` & OTLP | Tần suất request (req/s), HTTP 2xx/4xx/5xx status breakdown, P50/P95/P99 latency | `pv_forecasting_infra_service.json` |
| **3. Chất Lượng Dữ Liệu (Data Quality)** | `solar-ingest` batch task qua OTLP HTTP/Protobuf | Vi phạm schema (`pv_ingestion_schema_violations_total`), flag QC vật lý (`pv_ingestion_range_flags`), độ trễ dữ liệu (`pv_ingestion_data_lag_hours`) | `pv_forecasting_overview.json` |
| **4. Mô Hình & Độ Lệch (Model & Drift)** | `solar-evaluate`, `solar-drift`, `solar-retrain` qua OTLP | Live RMSE/MAE/R², tỷ lệ RMSE so với Champion (`pv_eval_rmse_ratio`), retrain flag (`pv_eval_needs_retrain`), điểm số KS drift, Target PSI, trạng thái job (`pv_batch_job_success`) | `pv_forecasting_overview.json` |

---

## 2. Đặc Tả Alert Rules (Provisioning)

Toàn bộ các quy tắc cảnh báo được khai báo tại `infra/monitoring/grafana/provisioning/alerting/rules.yaml`:
- **Datasource UID:** `grafanacloud-prom`
- **Folder UID:** `cos40007-pv-forecasting` (`COS40007 MLOps - PV Forecasting`)
- **Group Name:** `cos40007-mlops-alerts`
- **Evaluation Interval:** `1m`

### 2.1 Danh Mục 9 Alert Rules

| STT | Tên Rule / UID | Mức Độ (Severity) | Biểu Thức PromQL | Ngưỡng (Threshold) | Thời Gian Duy Trì (`for`) | Trạng Thái NoData / ExecErr |
|---|---|---|---|---|---|---|
| **1** | **Ingestion Failure or Schema Violation**<br>`fg160jrgozf9ce` *(kế thừa)* | `critical` | `pv_ingestion_schema_violations_total` | `> 0` | Ngay lập tức (last) | NoData: `OK`<br>ExecErr: `Alerting` |
| **2** | **Model nMAE Above Threshold (3 Consecutive Days)**<br>`bg160l41ewdtsb` *(kế thừa)* | `warning` | `pv_model_nmae{cohort="all"}` | `> 0.12` (12%) | `3d` (72 giờ liên tục) | NoData: `OK`<br>ExecErr: `OK` |
| **3** | **Feature Drift Above Threshold (Diagnostic Only)**<br>`eg160l41hea68d` *(kế thừa)* | `info` | `avg(drift_score{site="dkasc"})` | `> 0.25` | `1h` | NoData: `OK`<br>ExecErr: `OK` |
| **4** | **FastAPI Service Down**<br>`pv-api-down` *(mới)* | `critical` | `absent_over_time(http_requests_total{service="solar-api"}[5m])` | `> 0` (Metric biến mất) | `5m` | NoData: `Alerting`<br>ExecErr: `Alerting` |
| **5** | **FastAPI 5xx Error Rate Above 0.1%**<br>`pv-api-5xx-rate` *(mới)* | `warning` | `sum(rate(http_requests_total{service="solar-api",status=~"5.."}[10m])) / clamp_min(sum(rate(http_requests_total{service="solar-api"}[10m])), 1e-9)` | `> 0.001` (0.1%) | `10m` | NoData: `OK`<br>ExecErr: `OK` |
| **6** | **FastAPI P99 Latency Above 1s**<br>`pv-api-p99-latency` *(mới)* | `warning` | `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service="solar-api"}[10m])) by (le))` | `> 1.0s` *(Mục tiêu SLA Seminar 9 là **100ms**)* | `10m` | NoData: `OK`<br>ExecErr: `OK` |
| **7** | **PV Data Ingestion Stale**<br>`pv-data-ingest-stale` *(mới)* | `warning` | `(time() - last_over_time(pv_batch_job_last_run_timestamp_seconds{job_name="solar-ingest"}[8d])) / 3600` | `> 26h` (Chu kỳ 24h + 2h margin) | `10m` | NoData: `Alerting`<br>ExecErr: `Alerting` |
| **8** | **MLOps Batch Job Failed**<br>`pv-batch-job-failed` *(mới)* | `critical` | `last_over_time(pv_batch_job_success[26h])` | `< 1` (Giá trị bằng 0 theo từng `job_name`) | `5m` | NoData: `OK`<br>ExecErr: `Alerting` |
| **9** | **Composite Alert: Stale Ingest AND Model Degradation**<br>`pv-composite-dq-model` *(mới)* | `critical` | `((time() - last_over_time(pv_batch_job_last_run_timestamp_seconds{job_name="solar-ingest"}[8d])) / 3600 > 26) and on() (last_over_time(pv_eval_needs_retrain[26h]) == 1)` | `> 0` (Cả 2 điều kiện đồng thời thoả mãn) | `10m` | NoData: `OK`<br>ExecErr: `OK` |

### 2.2 Chi Tiết Về Mục Tiêu P99 Latency (Seminar 9)

- Theo yêu cầu và bài học từ **Seminar 9**, mục tiêu thời gian phản hồi (Target SLA) đối với endpoint dự báo thời gian thực D+1 (`/api/v1/forecast`) là dưới **100ms** (0.1s).
- Alert Rule số 6 đặt ngưỡng cảnh báo suy giảm hiệu năng nghiêm trọng là **1.0s** duy trì trong 10 phút liên tục (`for: 10m`). Mức 1.0s là ngưỡng bảo vệ an toàn giúp phát hiện nghẽn tài nguyên (CPU throttling, I/O database latency) hoặc sự cố OOM, đồng thời ngăn ngừa tình trạng báo động giả do spike ngắn hạn.

---

## 3. Bảng Điều Khiển "PV Forecast – API & Infrastructure"

File cấu hình: `infra/monitoring/grafana/dashboards/pv_forecasting_infra_service.json`
- **Dashboard UID:** `pv-forecasting-infra-service`
- **Dashboard Title:** `PV Forecast – API & Infrastructure`
- **Trường `id`:** Đã được loại bỏ (`null`) để hỗ trợ provisioning động không xung đột ID nội bộ.

### Các Panel Chính Được Cải Tiến
1. **Overview Stat Cards:**
   - **Slowest 5% Response Time (`P95 Latency`):** Đo đạc thời gian phản hồi ở phân vị 95 của API.
   - **Requests per Second (`req/s`):** Tổng thông lượng request được xử lý theo thời gian thực.
   - **Server Error Rate (`5xx %`):** Tỷ lệ phần trăm lỗi máy chủ 5xx (Threshold: Vàng 1%, Đỏ 5%).
   - **Average CPU Use:** Mức sử dụng CPU trung bình của cụm ECS Fargate.
2. **Forecast API Timeseries:**
   - **Response Time (P50 / P95 / P99):** Biểu đồ diễn tiến độ trễ đầy đủ bao gồm Median (P50), P95 và P99.
   - **Requests by Result (2xx / 4xx / 5xx & 5xx Error Rate %):** Biểu đồ thông lượng kết hợp trạng thái HTTP trên trục Y trái (req/s), tích hợp đường nét đứt màu đỏ trực quan hoá tỷ lệ lỗi 5xx (%) trên trục Y phải độc lập, tránh tạo thêm panel trùng lặp.
3. **Server Resources & Logs:**
   - **CPU by Service & Memory by Service:** Giám sát tài nguyên Fargate độc lập giữa `fastapi-service` và `mlflow-server`.
   - **Structured Prediction Logs:** Truy vấn log từ Grafana Cloud Loki (`{service_name="solar-api"}`).

---

## 4. Kiểm Thử và Xác Minh (Validation)

Hệ thống cung cấp kịch bản kiểm tra tự động `scripts/validate_monitoring.py` để xác minh:
1. Tính hợp lệ cú pháp của tất cả các file JSON (`pv_forecasting_infra_service.json`, `pv_forecasting_overview.json`).
2. Tính hợp lệ của YAML provisioning (`rules.yaml`, `dashboards.yml`).
3. Cú pháp PromQL và tính hiện hữu của tất cả các metrics trong cơ sở dữ liệu giám sát.

### Cách chạy kiểm tra (Validation):
Toàn bộ các file YAML và JSON có thể được kiểm thử cú pháp và biểu thức PromQL nhanh chóng bằng Python:
```bash
python -c "
import json, yaml, glob

# Validate JSON dashboards
for f in glob.glob('infra/monitoring/grafana/dashboards/*.json'):
    with open(f, encoding='utf-8') as fp:
        d = json.load(fp)
        assert d.get('uid'), f'Missing uid in {f}'
        print(f'[OK] JSON: {f} (UID: {d.get(\"uid\")})')

# Validate YAML provisioning
for f in glob.glob('infra/monitoring/grafana/provisioning/**/*.y*ml', recursive=True):
    with open(f, encoding='utf-8') as fp:
        y = yaml.safe_load(fp)
        assert y.get('apiVersion') == 1, f'Invalid apiVersion in {f}'
        print(f'[OK] YAML: {f}')
"
```
