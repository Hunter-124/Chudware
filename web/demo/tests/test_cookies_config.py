"""Signed session cookie and configuration validation."""

from __future__ import annotations

import pytest

from demo import cookies
from demo.config import ConfigError, DemoConfig, DEFAULT_MODELS

from .support import COOKIE_KEY, make_cfg

KEY = COOKIE_KEY


def test_cookie_round_trip_and_shape():
    sid = cookies.new_sid()
    value = cookies.sign(KEY, sid)
    assert cookies.SID_RE.fullmatch(sid)
    assert cookies.verify(KEY, value) == sid
    assert cookies.COOKIE_NAME.startswith("__Host-")


@pytest.mark.parametrize("mutate", [
    lambda v: v[:-1] + ("A" if v[-1] != "A" else "B"),          # flipped MAC character
    lambda v: ("0" if v[0] != "0" else "1") + v[1:],            # flipped sid character
    lambda v: v[:-5],                                           # truncated MAC
    lambda v: v + "x",                                          # extended MAC
    lambda v: v.replace(".", ":"),                              # wrong separator
    lambda v: v.upper(),                                        # case change
    lambda v: "",                                               # empty
    lambda v: "." + v.split(".")[1],                            # sid removed
    lambda v: v.split(".")[0] + ".",                            # MAC removed
    lambda v: v * 2,                                            # doubled
])
def test_tampered_cookie_is_rejected(mutate):
    value = cookies.sign(KEY, cookies.new_sid())
    assert cookies.verify(KEY, mutate(value)) is None


def test_cookie_from_another_key_or_sid_is_rejected():
    sid_a, sid_b = cookies.new_sid(), cookies.new_sid()
    value_a = cookies.sign(KEY, sid_a)
    assert cookies.verify(b"x" * 32, value_a) is None
    # A MAC is bound to its own sid: pairing it with another sid fails.
    assert cookies.verify(KEY, sid_b + "." + value_a.split(".")[1]) is None
    assert cookies.verify(KEY, None) is None
    assert cookies.verify(KEY, "a" * 500) is None


def test_sign_refuses_malformed_sid():
    with pytest.raises(ValueError):
        cookies.sign(KEY, "../../etc/passwd")


def test_default_config_values(tmp_path):
    cfg = make_cfg(tmp_path)
    assert (cfg.max_active, cfg.max_queue) == (3, 30)
    assert (cfg.idle_seconds, cfg.hard_seconds, cfg.warn_seconds) == (900, 2700, 300)
    assert cfg.nvidia_key == "" and cfg.nvidia_base == "https://integrate.api.nvidia.com/v1"
    assert cfg.ai_models == DEFAULT_MODELS and all(m.endswith(":free") for m in cfg.ai_models)
    assert cfg.pasta_options == ()


@pytest.mark.parametrize("model", ["anthropic/claude-opus-4", "qwen/qwen3-coder", "openai/gpt-oss-120b:free;x",
                                   "free", "vendor/name:free:extra"])
def test_only_free_models_are_accepted(tmp_path, model):
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, ai_models=(model,))


@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-mythos-5", "claude-opus-5", "claude-sonnet-4-6",
                                   "claude-haiku-4-5-20251001", "anthropic/claude-opus-5-5", "CLAUDE-OPUS-5-5"])
def test_only_claude_5_5_opus_sonnet_and_haiku_are_accepted(tmp_path, model):
    with pytest.raises(ConfigError, match="CLAUDE_MODELS"):
        make_cfg(tmp_path, claude_models=(model,))
    assert make_cfg(tmp_path).claude_models == ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5")


def test_claude_caps_come_from_the_environment_and_must_be_positive(tmp_path):
    cfg = DemoConfig.from_env({"CHUDWARE_DEMO_CLAUDE_SESSION_USD": "2.5", "CHUDWARE_DEMO_CLAUDE_MONTHLY_USD": "50",
                               "CHUDWARE_DEMO_CLAUDE_MODELS": "claude-haiku-5-5"})
    assert (cfg.claude_session_usd, cfg.claude_client_daily_usd, cfg.claude_daily_usd,
            cfg.claude_monthly_usd) == (2.5, 10.0, 30.0, 50.0)
    assert cfg.claude_models == ("claude-haiku-5-5",) and not cfg.claude_enabled
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, claude_daily_usd=0)
    with pytest.raises(ConfigError):
        DemoConfig.from_env({"CHUDWARE_DEMO_CLAUDE_SESSION_USD": "ten"})


@pytest.mark.parametrize("override", [
    {"warn_seconds": 3000},
    {"memory": "3500m,exec"},
    {"tmpfs_work": "1g,suid"},
    {"cookie_key": b"short"},
    {"max_active": 0},
    {"extra_security_opts": ("seccomp=unconfined",)},
    {"extra_security_opts": ("privileged=true",)},
    {"image": "evil image"},
    {"port_count": 2},
])
def test_dangerous_or_inconsistent_values_are_refused(tmp_path, override):
    with pytest.raises(ConfigError):
        make_cfg(tmp_path, **override)


def test_from_env_reads_prefixed_names(tmp_path):
    cfg = DemoConfig.from_env({
        "CHUDWARE_DEMO_OPENROUTER_KEY": "sk-or-x", "CHUDWARE_DEMO_COOKIE_KEY": "c" * 40,
        "CHUDWARE_DEMO_IMAGE_TAG": "sha-abc123", "CHUDWARE_DEMO_AI_MODELS": "a/b:free, c/d:free",
        "CHUDWARE_DEMO_NVIDIA_KEY": " nvapi-x ", "CHUDWARE_DEMO_MAX_ACTIVE": "2",
        "XDG_RUNTIME_DIR": str(tmp_path), "HOME": str(tmp_path),
    })
    assert cfg.ai_enabled and cfg.cookie_key_persistent
    assert cfg.image == "ghcr.io/hunter-124/chudware-web:sha-abc123"
    assert cfg.ai_models == ("a/b:free", "c/d:free")
    assert (cfg.nvidia_key, cfg.max_active) == ("nvapi-x", 2)
    assert cfg.session_root == tmp_path / "chudware-demo"


def test_missing_secrets_degrade_instead_of_failing(tmp_path):
    cfg = DemoConfig.from_env({"HOME": str(tmp_path)})
    assert not cfg.ai_enabled
    assert not cfg.cookie_key_persistent and len(cfg.cookie_key) == 32
    with pytest.raises(ConfigError):
        DemoConfig.from_env({"CHUDWARE_DEMO_MAX_ACTIVE": "many"})


def test_repr_hides_secrets(tmp_path):
    text = repr(make_cfg(tmp_path))
    assert "sk-or-owner-key" not in text and "kkkkkkkk" not in text
