"""Pytest wrapper for the formal edge-case state-machine sketches."""

import sys
from pathlib import Path

# Make the formal/ directory importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "formal"))

import edge_case_states as ecs


def test_generation_wraparound():
    ecs.test_generation_wraparound()


def test_post_reset_version():
    ecs.test_post_reset_version()


def test_descriptor_replay():
    ecs.test_descriptor_replay()


def test_multi_extent_all_or_none():
    ecs.test_multi_extent_all_or_none()


def test_pin_blocks_reclaim():
    ecs.test_pin_blocks_reclaim()
