"""Project-memory backend setting: explicit choice, legacy on/off, and junk values."""

from __future__ import annotations

import pytest

from services.agent.config import memory_backend, memory_enabled


@pytest.mark.parametrize(("cfg", "expected"), [
    ({"agent": {"memory_backend": "mnemo"}}, "mnemo"),
    ({"memory_backend": "off", "memory_enabled": True}, "off"),  # the choice wins
    # A config written before the choice existed keeps its meaning.
    ({"agent": {"memory_enabled": False}}, "off"),
    ({"memory_enabled": True}, "sharpshooter"),
    ({}, "sharpshooter"),
    ({"memory_backend": "hindsight", "memory_enabled": False}, "off"),  # unknown value
])
def test_memory_backend_setting(cfg, expected):
    assert memory_backend(cfg) == expected
    assert memory_enabled(cfg) is (expected != "off")
