"""Push batch-job results to Grafana Cloud as OTLP metrics.

The jobs (ingest, evaluate, drift, retrain) run as separate, short-lived ECS tasks,
so they cannot share the API's in-process metric store. Each job instead records its
results on a :class:`JobMetrics` object and exports them once, right before it exits,
over OTLP/HTTP (protobuf) to ``{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics``.

Export is a silent no-op when:
  * ``OTEL_SDK_DISABLED=true``,
  * ``OTEL_EXPORTER_OTLP_ENDPOINT`` is empty, or
  * no OTLP auth is configured (``OTEL_EXPORTER_OTLP_HEADERS``, or
    ``GRAFANA_CLOUD_API_TOKEN`` + ``GRAFANA_OTLP_INSTANCE_ID``).

Export failures are logged and swallowed: telemetry must never fail a job.

Naming follows Grafana Cloud's OTLP -> Prometheus translation: every metric here is a
gauge with no unit, so the exported name is exactly the name given (a unit would add a
suffix such as ``_seconds`` and a counter would gain ``_total``). Each data point also
carries ``service=<job name>``; ``service.name`` on the resource becomes the Prometheus
``job`` label, which is why no metric uses a ``job`` attribute of its own.

Usage::

    with job_metrics("solar-evaluate") as jm:
        jm.gauge("pv_eval_live_rmse", 0.081, {"champion_version": "3"})
        ...
    # on exit: pv_batch_job_duration_seconds / pv_batch_job_success /
    # pv_batch_job_last_run_timestamp_seconds are added,
    # everything is exported, flushed and the provider shut down.
"""

from __future__ import annotations

import base64
import logging
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Mapping, Optional
from urllib.parse import unquote

logger = logging.getLogger(__name__)

METER_NAME = "pv-forecasting-jobs"
EXPORT_TIMEOUT_S = 10.0

AttrValue = str | int | float | bool


def otlp_auth_headers(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """OTLP auth headers, parsed like the backend's ``services.telemetry._otlp_auth_headers``.

    ``OTEL_EXPORTER_OTLP_HEADERS`` is a comma-separated ``key=value`` list whose values
    are URL-decoded, so both ``Authorization=Basic <tok>`` and the W3C-encoded
    ``Authorization=Basic%20<tok>`` work. Falls back to building Basic auth from
    ``GRAFANA_OTLP_INSTANCE_ID`` + ``GRAFANA_CLOUD_API_TOKEN``.
    """
    env = os.environ if env is None else env
    headers: dict[str, str] = {}
    raw = (env.get("OTEL_EXPORTER_OTLP_HEADERS") or "").strip()
    if raw:
        for part in raw.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.strip():
                headers[key.strip()] = unquote(value.strip())
        return headers

    token = (env.get("GRAFANA_CLOUD_API_TOKEN") or "").strip()
    instance_id = (env.get("GRAFANA_OTLP_INSTANCE_ID") or "").strip()
    if token and instance_id:
        basic = base64.b64encode(f"{instance_id}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    return headers


def export_config(env: Mapping[str, str] | None = None) -> Optional[tuple[str, dict[str, str]]]:
    """Return ``(metrics_endpoint, headers)``, or None when export is disabled."""
    env = os.environ if env is None else env
    if (env.get("OTEL_SDK_DISABLED") or "").strip().lower() == "true":
        return None
    base = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip().rstrip("/")
    if not base:
        return None
    headers = otlp_auth_headers(env)
    if not headers.get("Authorization"):
        return None
    return f"{base}/v1/metrics", headers


@dataclass
class _Point:
    value: float
    attributes: dict[str, AttrValue]


@dataclass
class JobMetrics:
    """Collects gauge values during a job run and exports them once in :meth:`close`.

    Args:
        job_name: ECS task name, e.g. ``solar-evaluate``. Used as ``service.name``,
            the ``service`` attribute and the ``job_name`` attribute of the
            batch-job metrics.
        enabled: False records values but never exports (used for ``--dry-run``).
        reader: Optional metric reader to use instead of the OTLP exporter (tests).
    """

    job_name: str
    enabled: bool = True
    reader: object | None = None
    _points: dict[str, list[_Point]] = field(default_factory=dict)
    _descriptions: dict[str, str] = field(default_factory=dict)
    _closed: bool = False

    # ── Recording ──────────────────────────────────────────────────────────────

    def gauge(
        self,
        name: str,
        value: float | int | bool | None,
        attributes: Mapping[str, AttrValue | None] | None = None,
        description: str = "",
    ) -> None:
        """Record a gauge value. ``None`` / NaN / inf values are skipped (no fake data)."""
        try:
            if value is None:
                return
            fval = float(value)
            if not math.isfinite(fval):
                return
            attrs: dict[str, AttrValue] = {"service": self.job_name}
            for k, v in (attributes or {}).items():
                if v is not None:
                    attrs[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
            points = self._points.setdefault(name, [])
            # Last write wins for an identical attribute set, like a real gauge.
            points[:] = [p for p in points if p.attributes != attrs]
            points.append(_Point(fval, attrs))
            if description:
                self._descriptions.setdefault(name, description)
        except Exception as exc:
            logger.debug("Could not record metric %s: %s", name, exc)

    def values(self) -> dict[str, list[tuple[float, dict[str, AttrValue]]]]:
        """Recorded values (for logging and tests)."""
        return {n: [(p.value, dict(p.attributes)) for p in pts] for n, pts in self._points.items()}

    def record_run(self, duration_s: float, success: bool, finished_at: float | None = None) -> None:
        """Record the run's duration, outcome and end time (Unix seconds, default now)."""
        attrs = {"job_name": self.job_name}
        self.gauge(
            "pv_batch_job_duration_seconds", duration_s, attrs,
            "Wall-clock duration of the last batch job run in seconds",
        )
        self.gauge(
            "pv_batch_job_success", 1 if success else 0, attrs,
            "1 if the last batch job run finished without error, else 0",
        )
        self.gauge(
            "pv_batch_job_last_run_timestamp_seconds", time.time() if finished_at is None else finished_at, attrs,
            "Unix time (seconds) when the last batch job run finished, success or not",
        )

    # ── Export ─────────────────────────────────────────────────────────────────

    def close(self) -> bool:
        """Export everything recorded, then force_flush and shut down.

        Returns True if an export was attempted and reported success. Never raises.
        """
        if self._closed:
            return False
        self._closed = True
        if not self._points or not self.enabled:
            return False

        provider = None
        try:
            reader = self.reader
            if reader is None:
                cfg = export_config()
                if cfg is None:
                    logger.debug("OTLP metrics export disabled for %s.", self.job_name)
                    return False
                endpoint, headers = cfg
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                    OTLPMetricExporter,
                )
                from opentelemetry.sdk.metrics.export import (
                    PeriodicExportingMetricReader,
                )

                exporter = OTLPMetricExporter(endpoint=endpoint, headers=headers, timeout=EXPORT_TIMEOUT_S)
                # Infinite interval: no background thread, export only on force_flush.
                reader = PeriodicExportingMetricReader(
                    exporter,
                    export_interval_millis=math.inf,
                    export_timeout_millis=EXPORT_TIMEOUT_S * 1000,
                )

            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.resources import Resource

            resource = Resource.create({
                "service.name": self.job_name,
                # Stable per job (one task per job at a time), so each run updates the same
                # series instead of starting a new one; dashboards use last_over_time().
                "service.instance.id": self.job_name,
                "deployment.environment": "prod",
            })
            provider = MeterProvider(resource=resource, metric_readers=[reader])
            meter = provider.get_meter(METER_NAME)
            for name, points in self._points.items():
                instrument = meter.create_gauge(name, unit="", description=self._descriptions.get(name, ""))
                for p in points:
                    instrument.set(p.value, p.attributes)

            ok = bool(provider.force_flush(timeout_millis=EXPORT_TIMEOUT_S * 1000 * 2))
            if ok:
                logger.info("Exported %d metric series for %s.", sum(map(len, self._points.values())), self.job_name)
            else:
                logger.warning("OTLP metrics export for %s did not complete.", self.job_name)
            return ok
        except Exception as exc:
            logger.warning("OTLP metrics export for %s failed: %s", self.job_name, exc)
            return False
        finally:
            if provider is not None and self.reader is None:
                try:
                    provider.shutdown(timeout_millis=EXPORT_TIMEOUT_S * 1000)
                except Exception as exc:
                    logger.debug("MeterProvider shutdown failed: %s", exc)


@contextmanager
def job_metrics(job_name: str, enabled: bool = True, reader: object | None = None) -> Iterator[JobMetrics]:
    """Time a job run, record duration/success, and export on exit (even on failure).

    Exceptions from the job propagate unchanged; ``SystemExit`` with code 0 or 2 (the
    jobs' "ok" and "action needed" codes) counts as success.
    """
    jm = JobMetrics(job_name=job_name, enabled=enabled, reader=reader)
    start = time.monotonic()
    success = False
    try:
        yield jm
        success = True
    except SystemExit as exc:
        success = exc.code in (None, 0, 2)
        raise
    finally:
        try:
            jm.record_run(time.monotonic() - start, success)
        finally:
            jm.close()
