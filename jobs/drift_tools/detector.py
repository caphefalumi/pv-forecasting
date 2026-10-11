"""
detector.py — Statistical data drift detection.

Two complementary methods:

1. Kolmogorov-Smirnov (KS) test — per feature
   ─────────────────────────────────────────────
   Two-sample KS test comparing the empirical distributions of each numeric
   feature between the reference and current windows.

   A feature is flagged as drifted if: p-value < KS_ALPHA (default 0.05).

   Drift score = fraction of tested features that are flagged.
   Decision: drift_score >= KS_DRIFT_THRESHOLD (default 0.40 = 40%).

2. Population Stability Index (PSI) — target_norm only
   ─────────────────────────────────────────────────────
   PSI measures how much the distribution of the PV output has shifted.
   Reference window provides the baseline bin distribution.

   Standard PSI thresholds:
     PSI < 0.10  → no significant change
     0.10 – 0.20 → moderate change (monitor)
     PSI > 0.20  → significant change (DRIFT DETECTED)

Overall decision: DRIFT DETECTED if EITHER condition is true:
    (drift_score >= KS_DRIFT_THRESHOLD)  OR  (target_psi > PSI_THRESHOLD)

All thresholds are overridable via environment variables:
    DRIFT_KS_ALPHA          (default 0.05)
    DRIFT_KS_THRESHOLD      (default 0.40)
    DRIFT_PSI_THRESHOLD     (default 0.20)
    DRIFT_PSI_BINS          (default 10)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Default thresholds ─────────────────────────────────────────────────────────
DEFAULT_KS_ALPHA       = 0.05    # p-value threshold for KS test
DEFAULT_KS_THRESHOLD   = 0.40   # fraction of features drifted → DRIFT
DEFAULT_PSI_THRESHOLD  = 0.20   # PSI threshold → DRIFT
DEFAULT_PSI_BINS       = 10     # number of equal-width bins for PSI


@dataclass
class FeatureDriftResult:
    """KS test result for one feature."""
    feature:    str
    ks_stat:    float
    ks_pvalue:  float
    drifted:    bool     # True if p-value < KS_ALPHA
    ref_mean:   float
    cur_mean:   float
    mean_delta: float    # cur_mean - ref_mean
    ref_std:    float
    cur_std:    float


@dataclass
class DriftReport:
    """Aggregated drift detection result."""
    # Per-feature KS results
    feature_results: List[FeatureDriftResult] = field(default_factory=list)

    # Aggregated KS metrics
    n_features_tested:  int   = 0
    n_features_drifted: int   = 0
    drift_score:        float = 0.0    # fraction drifted

    # PSI on target_norm
    target_psi:         Optional[float] = None
    target_col:         str = "target_norm"

    # Overall decision
    ks_triggered:       bool = False   # drift_score >= threshold
    psi_triggered:      bool = False   # target_psi > threshold
    drift_detected:     bool = False   # ks_triggered OR psi_triggered

    # Human-readable summary
    reason:             str  = ""

    # Thresholds used
    ks_alpha:           float = DEFAULT_KS_ALPHA
    ks_threshold:       float = DEFAULT_KS_THRESHOLD
    psi_threshold:      float = DEFAULT_PSI_THRESHOLD

    # Window stats
    n_rows_reference:   int  = 0
    n_rows_current:     int  = 0


# ── PSI computation ────────────────────────────────────────────────────────────

def _compute_psi(
    reference: np.ndarray,
    current:   np.ndarray,
    n_bins:    int = DEFAULT_PSI_BINS,
) -> float:
    """Compute Population Stability Index (PSI).

    Uses equal-width bins derived from the reference distribution.
    Applies Laplace smoothing (ε = 1e-6) to avoid log(0).

    Args:
        reference: 1-D array of reference values.
        current:   1-D array of current values.
        n_bins:    Number of histogram bins (default: 10).

    Returns:
        PSI value (float). Higher = more drift.
    """
    eps = 1e-6

    # Build bins from reference range, extended slightly to catch edge values
    ref_min = reference.min()
    ref_max = reference.max()
    if ref_min == ref_max:
        # Degenerate case — no spread, PSI is undefined
        logger.warning("Reference has no variance — PSI set to 0.")
        return 0.0

    bins = np.linspace(ref_min, ref_max, n_bins + 1)
    bins[ 0] -= 1e-8   # include left edge
    bins[-1] += 1e-8   # include right edge

    ref_counts, _ = np.histogram(reference, bins=bins)
    cur_counts, _ = np.histogram(current,   bins=bins)

    ref_pct = ref_counts / max(ref_counts.sum(), 1)
    cur_pct = cur_counts / max(cur_counts.sum(), 1)

    # Laplace smoothing
    ref_pct = np.where(ref_pct == 0, eps, ref_pct)
    cur_pct = np.where(cur_pct == 0, eps, cur_pct)

    psi = float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))
    return psi


# ── KS test per feature ────────────────────────────────────────────────────────

def _run_ks_tests(
    df_reference:   pd.DataFrame,
    df_current:     pd.DataFrame,
    feature_cols:   List[str],
    ks_alpha:       float,
) -> List[FeatureDriftResult]:
    """Run two-sample KS test for each numeric feature.

    Returns a list of FeatureDriftResult, one per tested feature.
    """
    from scipy.stats import ks_2samp

    results: list[FeatureDriftResult] = []

    for col in feature_cols:
        if col not in df_reference.columns or col not in df_current.columns:
            logger.debug("Feature '%s' missing from one window — skipped.", col)
            continue

        ref_vals = df_reference[col].dropna().values.astype(float)
        cur_vals = df_current[col].dropna().values.astype(float)

        if len(ref_vals) < 2 or len(cur_vals) < 2:
            logger.debug("Feature '%s' has too few values — skipped.", col)
            continue

        ks_stat, ks_pvalue = ks_2samp(ref_vals, cur_vals)
        drifted = bool(ks_pvalue < ks_alpha)

        results.append(FeatureDriftResult(
            feature    = col,
            ks_stat    = float(ks_stat),
            ks_pvalue  = float(ks_pvalue),
            drifted    = drifted,
            ref_mean   = float(ref_vals.mean()),
            cur_mean   = float(cur_vals.mean()),
            mean_delta = float(cur_vals.mean() - ref_vals.mean()),
            ref_std    = float(ref_vals.std()),
            cur_std    = float(cur_vals.std()),
        ))

    return results


# ── Main detector ──────────────────────────────────────────────────────────────

def detect_drift(
    df_reference:    pd.DataFrame,
    df_current:      pd.DataFrame,
    feature_cols:    List[str] | None = None,
    target_col:      str = "target_norm",
    ks_alpha:        Optional[float] = None,
    ks_threshold:    Optional[float] = None,
    psi_threshold:   Optional[float] = None,
    psi_bins:        Optional[int]   = None,
) -> DriftReport:
    """Run full drift detection and return a DriftReport.

    Args:
        df_reference: Reference window DataFrame (daytime rows).
        df_current:   Current window DataFrame (daytime rows).
        feature_cols: Features to check with KS test.
                      Defaults to DRIFT_FEATURES from data_loader.
        target_col:   Target column for PSI computation.
        ks_alpha:     p-value threshold (env: DRIFT_KS_ALPHA, default 0.05).
        ks_threshold: Fraction-drifted threshold (env: DRIFT_KS_THRESHOLD, default 0.40).
        psi_threshold:PSI threshold (env: DRIFT_PSI_THRESHOLD, default 0.20).
        psi_bins:     Number of PSI bins (env: DRIFT_PSI_BINS, default 10).

    Returns:
        DriftReport with per-feature results and overall drift decision.
    """
    from jobs.drift_tools.data_loader import DRIFT_FEATURES

    # Resolve thresholds (env var > argument > default)
    ks_alpha      = ks_alpha      or float(os.environ.get("DRIFT_KS_ALPHA",      DEFAULT_KS_ALPHA))
    ks_threshold  = ks_threshold  or float(os.environ.get("DRIFT_KS_THRESHOLD",  DEFAULT_KS_THRESHOLD))
    psi_threshold = psi_threshold or float(os.environ.get("DRIFT_PSI_THRESHOLD", DEFAULT_PSI_THRESHOLD))
    psi_bins      = psi_bins      or int(os.environ.get("DRIFT_PSI_BINS",        DEFAULT_PSI_BINS))

    # Feature list: drop target_col from KS features (PSI handles it separately)
    if feature_cols is None:
        feature_cols = DRIFT_FEATURES
    ks_feature_cols = [c for c in feature_cols if c != target_col]

    report = DriftReport(
        target_col        = target_col,
        ks_alpha          = ks_alpha,
        ks_threshold      = ks_threshold,
        psi_threshold     = psi_threshold,
        n_rows_reference  = len(df_reference),
        n_rows_current    = len(df_current),
    )

    # ── KS tests ───────────────────────────────────────────────────────────────
    logger.info(
        "Running KS tests on %d features (alpha=%.2f) …",
        len(ks_feature_cols), ks_alpha,
    )
    report.feature_results   = _run_ks_tests(
        df_reference, df_current, ks_feature_cols, ks_alpha
    )
    report.n_features_tested  = len(report.feature_results)
    report.n_features_drifted = sum(r.drifted for r in report.feature_results)
    report.drift_score = (
        report.n_features_drifted / report.n_features_tested
        if report.n_features_tested > 0 else 0.0
    )

    logger.info(
        "KS results: %d / %d features drifted  (drift_score=%.2f, threshold=%.2f)",
        report.n_features_drifted, report.n_features_tested,
        report.drift_score, ks_threshold,
    )

    # ── PSI on target_norm ─────────────────────────────────────────────────────
    if target_col in df_reference.columns and target_col in df_current.columns:
        ref_target = df_reference[target_col].dropna().values.astype(float)
        cur_target = df_current[target_col].dropna().values.astype(float)

        if len(ref_target) >= 2 and len(cur_target) >= 2:
            report.target_psi = _compute_psi(ref_target, cur_target, psi_bins)
            logger.info(
                "PSI (%s): %.4f  (threshold=%.2f)",
                target_col, report.target_psi, psi_threshold,
            )
        else:
            logger.warning(
                "Not enough '%s' values for PSI computation.", target_col
            )
    else:
        logger.warning(
            "'%s' not found in one or both windows — PSI skipped.", target_col
        )

    # ── Overall drift decision ─────────────────────────────────────────────────
    report.ks_triggered  = report.drift_score  >= ks_threshold
    report.psi_triggered = (
        report.target_psi is not None and report.target_psi > psi_threshold
    )
    report.drift_detected = report.ks_triggered or report.psi_triggered

    # ── Build human-readable reason ────────────────────────────────────────────
    reasons: list[str] = []
    if report.ks_triggered:
        top_drifted = sorted(
            (r for r in report.feature_results if r.drifted),
            key=lambda r: r.ks_stat, reverse=True,
        )[:5]
        top_names = ", ".join(r.feature for r in top_drifted)
        reasons.append(
            f"{report.n_features_drifted}/{report.n_features_tested} features "
            f"drifted (score={report.drift_score:.2f} ≥ threshold={ks_threshold:.2f}). "
            f"Top drifted: {top_names}."
        )
    if report.psi_triggered:
        reasons.append(
            f"target_norm PSI={report.target_psi:.4f} > threshold={psi_threshold:.2f}."
        )
    if not report.drift_detected:
        reasons.append(
            f"No drift detected. drift_score={report.drift_score:.2f} < "
            f"{ks_threshold:.2f}"
            + (
                f",  target_norm PSI={report.target_psi:.4f} < {psi_threshold:.2f}"
                if report.target_psi is not None else ""
            )
            + "."
        )

    report.reason = " | ".join(reasons)

    if report.drift_detected:
        logger.warning("⚠️  DATA DRIFT DETECTED — %s", report.reason)
    else:
        logger.info("✅  NO SIGNIFICANT DRIFT — %s", report.reason)

    return report
