"""Config loader and validator for the PV data pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


# Default config path relative to project root
DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "pipeline.yaml"


class PipelineConfig:
    """Holds the validated pipeline configuration."""

    def __init__(self, data: dict, config_path: Path | None = None, data_root_override: str | None = None):
        self._data = data
        self.config_path = config_path

        # Resolve data_root: CLI override > config > cwd
        raw_root = data_root_override or data.get("data_root", "data")
        if config_path is not None:
            base = config_path.parent.parent  # repo root = parent of config/
        else:
            base = Path.cwd()
        self.data_root: Path = (base / raw_root).resolve()

        # Site
        site = data.get("site", {})
        self.latitude: float = float(site.get("latitude", -23.7624))
        self.longitude: float = float(site.get("longitude", 133.8754))
        self.altitude_m: float = float(site.get("altitude_m", 560))
        self.tz: str = site.get("tz", "Australia/Darwin")

        # DKASC
        dkasc = data.get("dkasc", {})
        self.yearly_files: list[str] = dkasc.get("yearly_files", [])
        self.api_dir: str = dkasc.get("api_dir", "dkasc_api_5min")
        self.api_name_map: str = dkasc.get("api_name_map", "dkasc_api_5min/source_names.csv")
        self.weather_source_id: int = int(dkasc.get("weather_source_id", 101))
        self.metadata_file: str = dkasc.get("metadata_file", "arrays_metadata_template.csv")
        self.overrides_file: str = dkasc.get("overrides_file", "array_overrides.csv")

        # Open-Meteo
        om = data.get("open_meteo", {})
        self.om_endpoint: str = om.get("endpoint", "https://previous-runs-api.open-meteo.com/v1/forecast")
        self.om_raw_dir: str = om.get("raw_dir", "open_meteo_raw")
        self.om_variables: list[str] = om.get("variables", [
            "shortwave_radiation", "direct_normal_irradiance", "diffuse_radiation",
            "temperature_2m", "cloud_cover", "wind_speed_10m",
        ])
        self.om_lead_suffix: str = om.get("lead_suffix", "_previous_day1")
        self.om_request_chunk: str = om.get("request_chunk", "year")
        self.om_sleep_s: float = float(om.get("sleep_s", 1))
        self.om_segments: list[dict] | None = om.get("segments", None)

        # Window
        window = data.get("window", {})
        self.window_start: str = str(window.get("start", "auto"))
        self.window_end: str = str(window.get("end", "2026-02-11"))

        # QC
        qc = data.get("qc", {})
        self.neg_clip_kw: float = float(qc.get("neg_clip_kw", -0.5))
        self.max_ratio_kwp: float = float(qc.get("max_ratio_kwp", 1.2))
        self.stuck_min_records: int = int(qc.get("stuck_min_records", 6))
        self.min_coverage: float = float(qc.get("min_coverage", 0.8333))
        self.daytime_zenith_max: float = float(qc.get("daytime_zenith_max", 85))
        self.near_zero_kwh_per_kwp_day: float = float(qc.get("near_zero_kwh_per_kwp_day", 0.5))

        # Physics
        physics = data.get("physics", {})
        self.physics_system_efficiency: float = float(physics.get("system_efficiency", 0.86))
        self.physics_t_ref_c: float = float(physics.get("t_ref_c", 25.0))
        self.physics_gamma_per_c: dict = physics.get("gamma_per_c", {})

        # Features
        features = data.get("features", {})
        self.pr_min_valid_hours_per_day: int = int(features.get("pr_min_valid_hours_per_day", 6))
        self.pr_windows: dict = features.get("pr_windows", {
            "pr_7d": {"days": 7, "min_days": 3},
            "pr_30d": {"days": 30, "min_days": 10},
        })

        # Splits
        splits = data.get("splits", {})
        self.splits_time = splits.get("time", None) if splits else None
        self.splits_array = splits.get("array", None) if splits else None
        self.coldstart_source_ids: list[int] = (
            self.splits_array.get("coldstart_source_ids", []) if isinstance(self.splits_array, dict) else []
        )
        self.recipes: dict = (
            self.splits_time.get("recipes", {}) if isinstance(self.splits_time, dict) else {}
        )

    def path(self, *parts: str) -> Path:
        """Return an absolute path under data_root."""
        return self.data_root.joinpath(*parts)

    def as_dict(self) -> dict:
        """Return the raw config dict (for manifest)."""
        return self._data


def load_config(config_path: Path | str | None = None, data_root_override: str | None = None) -> PipelineConfig:
    """Load and validate pipeline config from YAML.

    Args:
        config_path: Path to pipeline.yaml; defaults to config/pipeline.yaml relative to repo root.
        data_root_override: Override the data_root key from the config.

    Returns:
        PipelineConfig instance.
    """
    if config_path is None:
        config_path = DEFAULT_CONFIG
    config_path = Path(config_path).resolve()

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    return PipelineConfig(data, config_path=config_path, data_root_override=data_root_override)
