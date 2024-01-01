"""Pytest configuration: put the artifact root on sys.path.

Lets ``import simcxl_ext`` work under pytest without an editable install.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
