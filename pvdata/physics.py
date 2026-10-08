"""Physics baseline normalized power output (Champion 0).

Formula:
    p_phys_norm = max(0, poa_fc / 1000 * (1 + gamma[technology] * (temp_cell_fc - t_ref_c)) * system_efficiency)

Unit: kW per kWp.
NaN if poa_fc, temp_cell_fc, or gamma is missing / null.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from .config import PipelineConfig

logger = logging.getLogger(__name__)


def compute_p_phys_norm(
    poa_fc: pd.Series | np.ndarray,
    temp_cell_fc: pd.Series | np.ndarray,
    technology: pd.Series | np.ndarray,
    cfg: PipelineConfig,
) -> np.ndarray:
    """Compute normalized physics baseline power (kW / kWp).

    Args:
        poa_fc: Plane-of-array irradiance in W/m².
        temp_cell_fc: Cell temperature in °C.
        technology: PV cell technology name string (e.g. "poly-Si").
        cfg: Pipeline configuration containing physics parameters.

    Returns:
        np.ndarray of float32 normalized power in [0, inf) or NaN.
    """
    poa = np.asarray(poa_fc, dtype=np.float64)
    tcell = np.asarray(temp_cell_fc, dtype=np.float64)
    tech = pd.Series(technology).astype(str).str.strip()

    gamma_map = cfg.physics_gamma_per_c
    sys_eff = float(cfg.physics_system_efficiency)
    t_ref = float(cfg.physics_t_ref_c)

    # Map gamma for each row
    gamma_vals = tech.map(lambda x: gamma_map.get(x, None)).to_numpy(dtype=np.float64)

    # Valid mask: poa notna, tcell notna, gamma notna
    valid_mask = np.isfinite(poa) & np.isfinite(tcell) & np.isfinite(gamma_vals)

    res = np.full(len(poa), np.nan, dtype=np.float32)

    if np.any(valid_mask):
        p_sub = (
            (poa[valid_mask] / 1000.0)
            * (1.0 + gamma_vals[valid_mask] * (tcell[valid_mask] - t_ref))
            * sys_eff
        )
        # max(0, p_phys_norm)
        p_sub = np.clip(p_sub, 0.0, None)
        res[valid_mask] = p_sub.astype(np.float32)

    return res
