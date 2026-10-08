"""PV data pipeline package for DKASC + Open-Meteo training table."""

__version__ = "0.2.0"

from .dataset import Split, load_split, load_table

__all__ = ["Split", "load_split", "load_table"]
