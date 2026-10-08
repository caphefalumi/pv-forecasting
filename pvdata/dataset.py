"""Dataset loader for training recipes and splits.

Public API:
    load_split(version="latest", recipe="fleet_v1", groups=None, array_scope=None,
               as_of=None, data_root=None) -> Split
    load_table(version="latest", data_root=None) -> tuple[pd.DataFrame, dict]
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

from .config import DEFAULT_CONFIG, PipelineConfig, load_config

logger = logging.getLogger(__name__)

META_COLS = ["source_id", "ts_utc_end", "ts_local_start", "kwp", "p_phys_norm", "wx_source"]


@dataclass
class Split:
    """Container for recipe-based train/val/test splits."""
    X_train: Optional[pd.DataFrame] = None
    y_train: Optional[pd.Series] = None
    meta_train: Optional[pd.DataFrame] = None
    X_val: Optional[pd.DataFrame] = None
    y_val: Optional[pd.Series] = None
    meta_val: Optional[pd.DataFrame] = None
    X_test: Optional[pd.DataFrame] = None
    y_test: Optional[pd.Series] = None
    meta_test: Optional[pd.DataFrame] = None
    feature_names: List[str] = field(default_factory=list)
    target_name: str = "target_norm"
    metadata: Dict = field(default_factory=dict)


def _resolve_version_dir(version: str, data_root: Optional[Path | str] = None) -> Path:
    """Resolve the directory for a processed version (e.g. 'latest' or 'v1')."""
    if data_root is None:
        cfg = load_config()
        base_dir = cfg.path("processed")
    else:
        base_dir = Path(data_root) / "processed"

    if version == "latest":
        latest_file = base_dir / "latest.json"
        if not latest_file.exists():
            raise FileNotFoundError(f"latest.json not found in {base_dir}")
        with open(latest_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        resolved_version = data.get("version")
        if not resolved_version:
            raise ValueError("latest.json does not contain a valid 'version'")
        version_dir = base_dir / resolved_version
    else:
        version_dir = base_dir / version

    if not version_dir.exists():
        raise FileNotFoundError(f"Version directory not found: {version_dir}")

    return version_dir


def load_table(version: str = "latest", data_root: Optional[Path | str] = None) -> Tuple[pd.DataFrame, dict]:
    """Load the full train_table.parquet and feature_spec.json for a version."""
    vdir = _resolve_version_dir(version, data_root)
    tt_path = vdir / "train_table.parquet"
    spec_path = vdir / "feature_spec.json"

    if not tt_path.exists():
        raise FileNotFoundError(f"train_table.parquet not found in {vdir}")

    df = pd.read_parquet(tt_path)
    spec = {}
    if spec_path.exists():
        with open(spec_path, "r", encoding="utf-8") as fh:
            spec = json.load(fh)

    return df, spec


def load_split(
    recipe: str = "fleet_v1",
    version: str = "latest",
    groups: Optional[List[str]] = None,
    array_scope: Optional[str] = None,
    as_of: Optional[str | date] = None,
    data_root: Optional[Path | str] = None,
) -> Split:
    """Load train/val/test split according to a registered recipe.

    Args:
        recipe: Recipe name e.g. "fleet_v1", "fleet_v2", "v1_vs_v2", "rolling".
        version: "latest" or a specific version string.
        groups: Feature groups to include (defaults to recipe's feature_groups).
        array_scope: "train", "coldstart", or "all" (defaults to recipe's array_scope).
        as_of: Required if recipe == "rolling" (local date).
        data_root: Override for data_root path.

    Returns:
        Split dataclass instance.
    """
    vdir = _resolve_version_dir(version, data_root)
    df, spec = load_table(version=vdir.name, data_root=vdir.parent.parent)

    recipes_path = vdir / "recipes.json"
    if recipes_path.exists():
        with open(recipes_path, "r", encoding="utf-8") as fh:
            all_recipes = json.load(fh)
    else:
        cfg = load_config()
        all_recipes = cfg.recipes

    if recipe not in all_recipes:
        raise ValueError(f"Unknown recipe '{recipe}'. Available recipes: {list(all_recipes.keys())}")

    recipe_cfg = all_recipes[recipe]

    # 1. Apply row filter: target_norm.notna() & ghi_clear > 0 & ghi_fc.notna()
    row_filter = (
        df["target_norm"].notna()
        & (df["ghi_clear"] > 0)
        & df["ghi_fc"].notna()
    )
    df_filtered = df[row_filter].copy()

    # 2. Apply array scope: train, coldstart, or all (excluded arrays never returned)
    eff_scope = array_scope or recipe_cfg.get("array_scope", "train")
    if eff_scope == "train":
        df_filtered = df_filtered[df_filtered["array_split"] == "train"]
    elif eff_scope == "coldstart":
        df_filtered = df_filtered[df_filtered["array_split"] == "coldstart"]
    elif eff_scope == "all":
        df_filtered = df_filtered[df_filtered["array_split"].isin(["train", "coldstart"])]
    else:
        raise ValueError(f"Invalid array_scope '{eff_scope}'. Must be 'train', 'coldstart', or 'all'.")

    # 3. Resolve feature columns
    eff_groups = groups or recipe_cfg.get("feature_groups", ["base"])
    spec_groups = spec.get("feature_groups", {
        "base": [
            "ghi_fc", "dni_fc", "dhi_fc", "temp_fc", "cloud_fc", "wind_fc", "wx_source",
            "solar_zenith", "solar_azimuth", "ghi_clear", "kt_fc", "poa_fc", "temp_cell_fc",
            "p_phys_norm", "kwp", "technology_code", "tracking_code", "tilt_deg",
            "surface_azimuth_pvlib", "hour_sin", "hour_cos", "doy_sin", "doy_cos"
        ],
        "pr": ["pr_7d", "pr_30d"],
    })
    banned_cols = set(spec.get("banned", [
        "power_kw_mean", "coverage", "qc_flags", "target_kw", "target_norm", "array_no",
        "source_id", "wx_model", "is_daylight", "time_split", "array_split"
    ]))

    feat_cols: List[str] = []
    for g in eff_groups:
        if g not in spec_groups:
            raise ValueError(f"Unknown feature group '{g}'. Available: {list(spec_groups.keys())}")
        for col in spec_groups[g]:
            if col in banned_cols:
                raise ValueError(f"Feature column '{col}' is in banned list!")
            if col not in feat_cols:
                feat_cols.append(col)

    # 4. Filter by recipe date ranges
    local_dates = pd.to_datetime(df_filtered["ts_local_start"]).dt.date

    def _make_mask(rng) -> Optional[pd.Series]:
        if rng is None:
            return None
        s_date = date.fromisoformat(str(rng[0]))
        e_date = date.fromisoformat(str(rng[1]))
        return (local_dates >= s_date) & (local_dates <= e_date)

    if recipe == "rolling":
        if as_of is None:
            raise ValueError("Recipe 'rolling' requires an as_of date parameter.")
        as_of_dt = date.fromisoformat(str(as_of)) if isinstance(as_of, (str, Path)) else as_of
        test_days = int(recipe_cfg.get("test_days", 14))
        test_start = as_of_dt - timedelta(days=test_days - 1)
        mask_test = (local_dates >= test_start) & (local_dates <= as_of_dt)
        mask_train = (local_dates < test_start)
        mask_val = None
    else:
        mask_train = _make_mask(recipe_cfg.get("train"))
        mask_val = _make_mask(recipe_cfg.get("val"))
        mask_test = _make_mask(recipe_cfg.get("test"))

    def _build_part(mask: Optional[pd.Series]) -> Tuple[Optional[pd.DataFrame], Optional[pd.Series], Optional[pd.DataFrame]]:
        if mask is None or not mask.any():
            return None, None, None
        sub = df_filtered[mask]
        X = sub[feat_cols].copy()
        y = sub["target_norm"].copy()
        avail_meta = [c for c in META_COLS if c in sub.columns]
        meta = sub[avail_meta].copy()
        return X, y, meta

    X_train, y_train, meta_train = _build_part(mask_train)
    X_val, y_val, meta_val = _build_part(mask_val)
    X_test, y_test, meta_test = _build_part(mask_test)

    meta_dict = {
        "recipe": recipe,
        "version": vdir.name,
        "array_scope": array_scope,
        "groups": groups,
        "n_rows_total": len(df),
        "n_rows_usable": len(df_filtered),
    }

    return Split(
        X_train=X_train,
        y_train=y_train,
        meta_train=meta_train,
        X_val=X_val,
        y_val=y_val,
        meta_val=meta_val,
        X_test=X_test,
        y_test=y_test,
        meta_test=meta_test,
        feature_names=feat_cols,
        target_name="target_norm",
        metadata=meta_dict,
    )
