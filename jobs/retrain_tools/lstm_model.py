"""
lstm_model.py — PyTorch LSTM fleet model for PV output forecasting.

Architecture:
  Input:  (batch, window_size, n_features)  — sliding 6-hour window
  LSTM:   2 layers, hidden_size=128, dropout=0.2
  Head:   Linear(128 → 1) → ReLU clamp [0, inf)
  Output: scalar target_norm prediction for the last timestep

Window construction:
  For each (source_id, local_date) group, overlapping windows of length
  window_size are extracted.  Windows that contain any NaN feature value
  are discarded.  The last timestep's target_norm is the prediction target.

Preprocessing:
  A sklearn Pipeline (SimpleImputer → StandardScaler) is fit on training
  features only and stored inside the checkpoint.

Training:
  Optimizer: Adam, lr=1e-3
  Loss:      MSELoss
  Scheduler: ReduceLROnPlateau (factor=0.5, patience=5)
  Early stop: patience=10 epochs on val_loss
  Best model (lowest val_loss) is saved.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Hyperparameter defaults ────────────────────────────────────────────────────

WINDOW_SIZE  = 6     # consecutive daytime hours per array per window
HIDDEN_SIZE  = 128
NUM_LAYERS   = 2
DROPOUT      = 0.2
BATCH_SIZE   = 512
LR           = 1e-3
NUM_EPOCHS   = 50
ES_PATIENCE  = 10    # early stopping patience (epochs)
LR_PATIENCE  = 5     # ReduceLROnPlateau patience


@dataclass
class TrainConfig:
    """Configurable hyperparameters for LSTM training."""
    window_size: int   = WINDOW_SIZE
    hidden_size: int   = HIDDEN_SIZE
    num_layers:  int   = NUM_LAYERS
    dropout:     float = DROPOUT
    batch_size:  int   = BATCH_SIZE
    lr:          float = LR
    num_epochs:  int   = NUM_EPOCHS
    es_patience: int   = ES_PATIENCE
    lr_patience: int   = LR_PATIENCE

    def as_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)


@dataclass
class TrainHistory:
    """Training loss history."""
    train_losses: List[float] = field(default_factory=list)
    val_losses:   List[float] = field(default_factory=list)
    best_epoch:   int = 0
    best_val_loss: float = float("inf")


# ── Dataset ────────────────────────────────────────────────────────────────────

def _build_windows(
    df: pd.DataFrame,
    feature_cols: List[str],
    target_col: str,
    window_size: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Build (X, y) window arrays from a raw DataFrame.

    For each (source_id, local_date) group:
      - Sort rows by ts_utc_end.
      - Extract overlapping windows of length window_size.
      - Skip windows with any NaN in features or target.

    Returns:
        X: float32 array of shape (n_windows, window_size, n_features)
        y: float32 array of shape (n_windows,)
    """
    ts = pd.to_datetime(df["ts_local_start"])
    df = df.copy()
    df["_local_date"] = ts.dt.date

    X_list, y_list = [], []

    for (sid, day), grp in df.groupby(["source_id", "_local_date"], sort=False):
        grp = grp.sort_values("ts_utc_end").reset_index(drop=True)
        if len(grp) < window_size:
            continue

        feat = grp[feature_cols].values.astype(np.float32)  # (n_hours, F)
        tgt  = grp[target_col].values.astype(np.float32)    # (n_hours,)

        for i in range(len(grp) - window_size + 1):
            x_win = feat[i: i + window_size]   # (W, F)
            y_val = tgt[i + window_size - 1]   # scalar: predict last step

            # Discard windows with NaN values
            if np.any(~np.isfinite(x_win)) or not np.isfinite(y_val):
                continue

            X_list.append(x_win)
            y_list.append(y_val)

    if not X_list:
        raise RuntimeError(
            "No valid windows could be built. "
            "Check that feature columns are present and contain finite values."
        )

    X = np.array(X_list, dtype=np.float32)
    y = np.array(y_list, dtype=np.float32)
    logger.debug("Built %d windows from group.", len(X))
    return X, y


def build_datasets(
    df_train: pd.DataFrame,
    df_val:   pd.DataFrame,
    df_test:  pd.DataFrame,
    feature_cols: List[str],
    target_col: str = "target_norm",
    window_size: int = WINDOW_SIZE,
) -> Tuple[
    Tuple[np.ndarray, np.ndarray],
    Tuple[np.ndarray, np.ndarray],
    Tuple[np.ndarray, np.ndarray],
    object,   # fitted sklearn preprocessor
]:
    """Build window arrays and fit a feature preprocessor on training data.

    Returns:
        (X_train, y_train), (X_val, y_val), (X_test, y_test), preprocessor
    """
    try:
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.impute import SimpleImputer
    except ImportError as exc:
        raise ImportError("scikit-learn is required: pip install scikit-learn") from exc

    logger.info("Building training windows (window_size=%d) …", window_size)
    X_tr_raw, y_tr = _build_windows(df_train, feature_cols, target_col, window_size)
    logger.info("Building validation windows …")
    X_va_raw, y_va = _build_windows(df_val,   feature_cols, target_col, window_size)
    logger.info("Building test windows …")
    X_te_raw, y_te = _build_windows(df_test,  feature_cols, target_col, window_size)

    logger.info(
        "Windows: train=%d | val=%d | test=%d",
        len(X_tr_raw), len(X_va_raw), len(X_te_raw),
    )

    # Fit preprocessor on training data only (flatten → fit → reshape)
    n_tr, W, F = X_tr_raw.shape
    preprocessor = Pipeline([
        ("imputer", SimpleImputer(strategy="median")),
        ("scaler",  StandardScaler()),
    ])
    X_tr_flat = preprocessor.fit_transform(X_tr_raw.reshape(-1, F))
    X_va_flat = preprocessor.transform(X_va_raw.reshape(-1, F))
    X_te_flat = preprocessor.transform(X_te_raw.reshape(-1, F))

    X_tr = X_tr_flat.reshape(n_tr, W, F).astype(np.float32)
    X_va = X_va_flat.reshape(len(X_va_raw), W, F).astype(np.float32)
    X_te = X_te_flat.reshape(len(X_te_raw), W, F).astype(np.float32)

    return (X_tr, y_tr), (X_va, y_va), (X_te, y_te), preprocessor


# ── Model ─────────────────────────────────────────────────────────────────────

def _get_torch():
    try:
        import torch
        import torch.nn as nn
        return torch, nn
    except ImportError as exc:
        raise ImportError(
            "PyTorch is required: pip install torch\n"
            "See https://pytorch.org/get-started/locally/ for platform-specific wheels."
        ) from exc


class PVLSTMModel:
    """Thin wrapper around the PyTorch LSTM module."""

    def __init__(
        self,
        input_size:  int,
        hidden_size: int = HIDDEN_SIZE,
        num_layers:  int = NUM_LAYERS,
        dropout:     float = DROPOUT,
    ):
        torch, nn = _get_torch()
        self.cfg = dict(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            dropout=dropout,
        )
        self._module = _LSTMModule(input_size, hidden_size, num_layers, dropout)

    def parameters(self):
        return self._module.parameters()

    def module(self):
        return self._module

    def __call__(self, x):
        return self._module(x)


def _make_module(input_size, hidden_size, num_layers, dropout):
    """Construct the nn.Module (deferred import of torch)."""
    torch, nn = _get_torch()

    class _Inner(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout if num_layers > 1 else 0.0,
            )
            self.head = nn.Linear(hidden_size, 1)

        def forward(self, x):
            # x: (batch, seq, features)
            out, _ = self.lstm(x)
            last = out[:, -1, :]          # last timestep
            pred = self.head(last).squeeze(-1)
            if not self.training:
                return torch.clamp(pred, min=0.0)  # target_norm ≥ 0 at inference
            return pred

    return _Inner()


# ── Training loop ─────────────────────────────────────────────────────────────

def train_model(
    train_data: Tuple[np.ndarray, np.ndarray],
    val_data:   Tuple[np.ndarray, np.ndarray],
    feature_cols: List[str],
    cfg: TrainConfig = None,
    checkpoint_path: Optional[Path] = None,
) -> Tuple[object, TrainHistory]:
    """Train the LSTM model with early stopping.

    Args:
        train_data:       (X_train, y_train) numpy arrays.
        val_data:         (X_val, y_val) numpy arrays.
        feature_cols:     Feature column names (used for config logging).
        cfg:              TrainConfig (uses defaults if None).
        checkpoint_path:  If set, save the best model state_dict here.

    Returns:
        (best_module, history)
    """
    torch, nn = _get_torch()
    from torch.utils.data import TensorDataset, DataLoader

    cfg = cfg or TrainConfig()
    X_tr, y_tr = train_data
    X_va, y_va = val_data

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    logger.info("Training device: %s", device)

    input_size = X_tr.shape[-1]
    model = _make_module(input_size, cfg.hidden_size, cfg.num_layers, cfg.dropout)
    model.to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=cfg.lr)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, mode="min", factor=0.5, patience=cfg.lr_patience
    )
    criterion = nn.MSELoss()

    train_ds = TensorDataset(
        torch.tensor(X_tr), torch.tensor(y_tr)
    )
    val_ds = TensorDataset(
        torch.tensor(X_va), torch.tensor(y_va)
    )
    train_loader = DataLoader(
        train_ds, batch_size=cfg.batch_size, shuffle=True, drop_last=False
    )
    val_loader = DataLoader(
        val_ds, batch_size=cfg.batch_size * 2, shuffle=False
    )

    history = TrainHistory()
    no_improve = 0
    best_state = None

    for epoch in range(1, cfg.num_epochs + 1):
        # ── Train ──────────────────────────────────────────────────────────
        model.train()
        running_loss = 0.0
        for xb, yb in train_loader:
            xb, yb = xb.to(device), yb.to(device)
            optimizer.zero_grad()
            pred = model(xb)
            loss = criterion(pred, yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            running_loss += loss.item() * len(xb)
        train_loss = running_loss / len(train_ds)

        # ── Validate ───────────────────────────────────────────────────────
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for xb, yb in val_loader:
                xb, yb = xb.to(device), yb.to(device)
                pred = model(xb)
                val_loss += criterion(pred, yb).item() * len(xb)
        val_loss /= len(val_ds)

        history.train_losses.append(train_loss)
        history.val_losses.append(val_loss)
        scheduler.step(val_loss)

        if val_loss < history.best_val_loss:
            history.best_val_loss = val_loss
            history.best_epoch    = epoch
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            no_improve = 0
        else:
            no_improve += 1

        if epoch % 5 == 0 or epoch == 1:
            logger.info(
                "Epoch %3d/%d  train_loss=%.5f  val_loss=%.5f  best=%.5f",
                epoch, cfg.num_epochs, train_loss, val_loss, history.best_val_loss,
            )

        if no_improve >= cfg.es_patience:
            logger.info(
                "Early stopping at epoch %d (no improvement for %d epochs)",
                epoch, cfg.es_patience,
            )
            break

    # Restore best weights
    if best_state is not None:
        model.load_state_dict(best_state)

    logger.info(
        "Training complete — best epoch: %d, best val_loss: %.5f",
        history.best_epoch, history.best_val_loss,
    )

    if checkpoint_path:
        torch.save(best_state, checkpoint_path)
        logger.info("Model state_dict saved to %s", checkpoint_path)

    model.eval()
    return model, history


def build_checkpoint(
    model,         # nn.Module with best weights loaded
    preprocessor,  # sklearn Pipeline
    feature_cols: List[str],
    cfg: TrainConfig,
) -> dict:
    """Bundle everything needed for inference into a single dict."""
    import torch
    return {
        "model_state_dict": model.state_dict(),
        "model_config": {
            "input_size":  len(feature_cols),
            "hidden_size": cfg.hidden_size,
            "num_layers":  cfg.num_layers,
            "dropout":     cfg.dropout,
        },
        "preprocessor": preprocessor,
        "feature_cols": feature_cols,
        "window_size":  cfg.window_size,
    }


def save_checkpoint(checkpoint: dict, path: Path) -> None:
    """Save checkpoint dict to a .pt file."""
    import torch
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(checkpoint, path)
    size_mb = path.stat().st_size / 1e6
    logger.info("Checkpoint saved: %s (%.2f MB)", path.name, size_mb)


def load_checkpoint(path: Path) -> dict:
    """Load a checkpoint dict from a .pt file."""
    import torch
    return torch.load(path, map_location="cpu", weights_only=False)


def predict_from_checkpoint(
    checkpoint: dict,
    X_raw: np.ndarray,  # (N, n_features) raw un-scaled features
) -> np.ndarray:
    """Run inference: preprocess → LSTM → return target_norm predictions."""
    import torch
    cfg_dict = checkpoint["model_config"]
    preprocessor = checkpoint["preprocessor"]
    state_dict = checkpoint["model_state_dict"]

    model = _make_module(**cfg_dict)
    model.load_state_dict(state_dict)
    model.eval()

    window_size = checkpoint.get("window_size", WINDOW_SIZE)
    F = cfg_dict["input_size"]

    # Preprocess (N, F) then reshape to (1, window_size, F) per sample
    X_scaled = preprocessor.transform(X_raw).astype(np.float32)

    with torch.no_grad():
        t = torch.tensor(X_scaled).unsqueeze(0)   # trivial: treat whole as one window
        # For single-step inference, caller must handle windowing externally
        pred = model(t)
    return pred.numpy()
