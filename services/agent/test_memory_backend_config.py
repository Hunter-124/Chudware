"""Project-memory backend setting: explicit choice, legacy on/off, and junk values."""

from __future__ import annotations

import pytest

from services.agent.config import memory_backend, memory_enabled


@pytest.mark.parametrize(("cfg", "expected"), [
    ({"agent": {"memory_backend": "mnemo"}}, "mnemopi"),
    ({"memory_backend": "local"}, "local"),
    ({"memory_backend": "off", "memory_enabled": True}, "off"),  # the choice wins
    # A config written before the choice existed keeps its meaning.
    ({"agent": {"memory_enabled": False}}, "off"),
    ({"memory_enabled": True}, "sharpshooter"),
    ({}, "sharpshooter"),
    ({"memory_backend": "hindsight", "memory_enabled": False}, "hindsight"),
])
def test_memory_backend_setting(cfg, expected):
    assert memory_backend(cfg) == expected
    assert memory_enabled(cfg) is (expected != "off")


def test_unknown_memory_backend_is_not_substituted():
    with pytest.raises(ValueError, match="Unsupported memory backend"):
        memory_backend({"memory_backend": "not-a-backend"})
