# MLOps Lifecycle, Retraining & Versioning

| | |
|---|---|
| **Registered Model** | `pv-lstm-fleet` |
| **MLflow Experiments** | `pv-lstm-fleet` (training), `pv-lstm-fleet-eval` (evaluation), `pv-lstm-fleet-drift` (drift) |
| **Registry Aliases** | `@champion` (production serving), `@candidate` (verified challenger awaiting approval) |

---

## 1. Data & Training Strategy

### 1.1 Two-Axis Splitting (Time & Array)
Random splitting is never used because neighboring hours exhibit strong temporal autocorrelation (median $r = 0.83$ hour-to-hour and $r = 0.59$ day-to-day). Data are split along two axes:

1. **Time Axis (Development Rounds):**

| Round | Training Window | Validation Window | Test Window |
|---|---|---|---|
| `fleet_v1` | 20 Jan 2024 – 31 Dec 2024 | 1 Jan 2025 – 30 Apr 2025 | 1 May 2025 – 1 Oct 2025 |
| `fleet_v2` | 20 Jan 2024 – 31 Dec 2025 | 1 Jan 2026 – 30 Apr 2026 | 1 May 2026 – 1 Oct 2026 |
| `v1_vs_v2` | — | — | Both evaluated on 1 May 2026 – 1 Oct 2026 |

2. **Array Axis (Cold-Start Generalization):**
   - **25 Training Arrays:** Used for fitting the fleet model.
   - **6 Held-Out Cold-Start Arrays:** Never included in training (covering east/west-facing panels `16C` and `16D`, three cell technologies, and under-performing array `20`). Their evaluation error estimates the cold-start error experienced by a newly onboarded customer system.

### 1.2 Target Normalization & Accuracy Metric
The forecasting target is hourly active power normalized by installed rated capacity:
$$\text{target\_norm} = \frac{P_{\text{actual}} (\text{kW})}{P_{\text{installed}} (\text{kWp})}$$

Primary accuracy is evaluated via **Normalized Mean Absolute Error (nMAE)**:
$$\text{nMAE} = \frac{\frac{1}{n}\sum_{i=1}^n |\hat{y}_i - y_i|}{P_{\text{installed}}}$$
alongside RMSE and $R^2$ on `target_norm`. Normalization by installed capacity makes errors directly comparable across sites of different sizes, while avoiding the zero-division instability of MAPE during dawn, dusk, and heavy overcast hours.

---

## 2. Automated MLOps Batch Jobs

Four containerized batch jobs share the root `Dockerfile` image and execute as serverless ECS Fargate tasks orchestrated by Amazon EventBridge Scheduler and AWS Step Functions:

| Job | Task Definition | Entrypoint | Schedule (UTC) | Role |
|---|---|---|---|---|
| **Ingest** | `solar-ingest` | `python -m jobs.ingest` (`ingest.py`) | Daily `01:00` | Fetches new DKASC 5-min power & Open-Meteo D+1 forecasts, runs QC & `pvlib` feature engineering, exports monthly CSVs to Supabase/S3, and commits updated `.dvc` pointers. |
| **Evaluate** | `solar-evaluate` | `python -m jobs.evaluate` (`evaluate.py`) | Daily `03:00` | Evaluates the active `@champion` model against the latest ground-truth records, logs live RMSE/MAE/$R^2$ to MLflow (`pv-lstm-fleet-eval`), and emits exit code `2` if degradation exceeds threshold. |
| **Drift** | `solar-drift` | `python -m jobs.drift_check` (`drift.py`) | Daily `04:00` | Compares the recent 7-day daytime window against the preceding 30-day reference window using per-feature Kolmogorov–Smirnov (KS) tests and target PSI, logging the `.md` drift report to MLflow and S3. |
| **Retrain** | `solar-retrain` | `python -m jobs.retrain` (`retrain.py`) | Weekly (Sun `02:00`) or Early Trigger | Trains a new Challenger LSTM model on the latest DVC dataset version, compares against `@champion`, uploads `checkpoint.pt` to `artifacts/<run_id>/`, and assigns `@candidate` if improved. |

---

## 3. Hybrid Retraining Strategy & Human-in-the-Loop Promotion

### 3.1 Retraining Triggers
The platform combines two complementary triggers:
1. **Time-Based Trigger (Weekly):** Scheduled weekly via EventBridge (`cron(0 2 ? * SUN *)`) to incorporate the latest production records and guard against gradual seasonal or panel-aging drift.
2. **Performance-Based Trigger (Concept Drift):** Triggered when live model error on reference sites degrades beyond threshold. Covariate weather drift (`solar-drift`) is treated as **diagnostic only** and never triggers retraining on its own, preventing false-positive retrains during normal seasonal transitions.

### 3.2 Challenger vs. Champion & Promotion Gate
1. **Training:** `python retrain.py` trains a 2-layer PyTorch LSTM (`window_size=6`, `hidden_size=128`, `num_layers=2`, `dropout=0.2`) with `ReduceLROnPlateau` and early stopping on validation loss.
2. **Comparison:** If the Challenger achieves a lower test error than the current `@champion` (or if no `@champion` exists yet), it is assigned the `@candidate` alias in MLflow Model Registry and its `checkpoint.pt` bundle (weights + fitted `scikit-learn` preprocessor + feature spec) is uploaded to Supabase/S3 `artifacts/<run_id>/checkpoint.pt`.
3. **Human Approval & Promotion (`promote.py`):**
   - A team member reviews the Discord notification and MLflow run metrics.
   - Running `python promote.py --version <N> --approver <username>` transitions the `@champion` alias to version `<N>`, records `promoted_by` and `promoted_at` provenance tags in MLflow, and notifies the team on Discord.
   - The live FastAPI service (`solar-api`) automatically detects the new `@champion` alias within 60 seconds (or immediately via `POST /api/v1/model/reload`) without requiring a container redeploy.

### 3.3 Model Rollback
If a newly promoted model exhibits unexpected behavior in production, rollback is instantaneous and requires no code deployment:
```bash
python promote.py --version <PREVIOUS_VERSION> --approver <username>
```
This re-points the `@champion` alias in MLflow Registry back to the previous model version, and `solar-api` hot-reloads the prior checkpoint on its next refresh cycle.
