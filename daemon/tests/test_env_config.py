#!/usr/bin/env python3
"""Unit tests for .env-based settings.

Covers the parser, the precedence chain (real env > .env > config file >
default) and the settings wired through it.

Run: python -m pytest daemon/tests/test_env_config.py -x -q
"""
from pathlib import Path

import pytest

import daemon.claude_usage_daemon as mod


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    """Point the loader at a throwaway .env and clear its cache."""
    path = tmp_path / ".env"
    monkeypatch.setattr(mod, "ENV_FILE", path)
    monkeypatch.setattr(mod, "_env_cache", None)
    monkeypatch.setattr(mod, "_env_mtime", None)
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "absent-config")
    for key in list(mod.os.environ):
        if key.startswith(mod.ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_missing_env_file_is_not_an_error(env_file):
    assert mod._load_env_file() == {}
    assert mod.env_setting("clock") is None


def test_parses_values_comments_and_blank_lines(env_file):
    env_file.write_text(
        "\n"
        "# a comment\n"
        "CLAWDMETER_CLOCK=24\n"
        "\n"
        "   CLAWDMETER_CHIME = on   \n"
        "# CLAWDMETER_TICK=99\n"
    )
    assert mod.env_setting("clock") == "24"
    assert mod.env_setting("chime") == "on"
    assert mod.env_setting("tick") is None    # commented out


def test_strips_export_prefix_and_quotes(env_file):
    env_file.write_text(
        'export CLAWDMETER_DEVICE_NAME="My Meter"\n'
        "CLAWDMETER_CLOCK='12'\n"
    )
    assert mod.env_setting("device_name") == "My Meter"
    assert mod.env_setting("clock") == "12"


def test_strips_trailing_comment(env_file):
    env_file.write_text("CLAWDMETER_POLL_INTERVAL=30 # faster refresh\n")
    assert mod.env_setting("poll_interval") == "30"


def test_blank_value_reads_as_unset(env_file):
    env_file.write_text("CLAWDMETER_CLOCK=\nCLAWDMETER_CHIME=   \n")
    assert mod.env_setting("clock") is None
    assert mod.env_setting("chime") is None


def test_file_is_reread_when_it_changes(env_file):
    env_file.write_text("CLAWDMETER_CLOCK=12\n")
    assert mod.env_setting("clock") == "12"
    import os
    st = env_file.stat()
    env_file.write_text("CLAWDMETER_CLOCK=24\n")
    os.utime(env_file, (st.st_atime + 5, st.st_mtime + 5))   # ensure mtime moves
    assert mod.env_setting("clock") == "24"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

def test_real_environment_beats_the_env_file(env_file, monkeypatch):
    """A launchd/systemd unit must be able to override the file."""
    env_file.write_text("CLAWDMETER_CLOCK=12\n")
    monkeypatch.setenv("CLAWDMETER_CLOCK", "24")
    assert mod.env_setting("clock") == "24"


def test_env_file_beats_the_config_file(env_file):
    cfg = env_file.parent / "config"
    cfg.write_text("clock = 12\n")
    mod.CONFIG_FILE = cfg
    env_file.write_text("CLAWDMETER_CLOCK=24\n")
    assert mod.read_clock_setting() == "24"


def test_config_file_still_used_when_env_absent(env_file):
    cfg = env_file.parent / "config"
    cfg.write_text("clock = 12\nchime = on\n")
    mod.CONFIG_FILE = cfg
    assert mod.read_clock_setting() == "12"
    assert mod.read_chime_setting() == "on"


# ---------------------------------------------------------------------------
# Settings wired through it
# ---------------------------------------------------------------------------

def test_config_dirs_from_env(env_file):
    env_file.write_text("CLAWDMETER_CONFIG_DIRS=~/.claude, ~/.claude-side\n")
    assert mod.read_config_dirs() == [Path.home() / ".claude",
                                      Path.home() / ".claude-side"]


def test_config_dirs_defaults_when_unset(env_file):
    assert mod.read_config_dirs() == [mod.DEFAULT_CONFIG_DIR]


def test_chime_accepts_truthy_spellings(env_file):
    for raw, want in (("on", "on"), ("true", "on"), ("1", "on"),
                      ("yes", "on"), ("off", "off"), ("nonsense", "off")):
        env_file.write_text(f"CLAWDMETER_CHIME={raw}\n")
        mod._env_cache = None
        mod._env_mtime = None
        assert mod.read_chime_setting() == want, raw


def test_clock_rejects_an_invalid_value(env_file):
    """An unrecognised clock value must not reach the firmware."""
    env_file.write_text("CLAWDMETER_CLOCK=sundial\n")
    assert mod.read_clock_setting() == "off"


# ---------------------------------------------------------------------------
# env_number
# ---------------------------------------------------------------------------

def test_env_number_parses_and_falls_back(env_file):
    env_file.write_text("CLAWDMETER_POLL_INTERVAL=30\nCLAWDMETER_TICK=abc\n")
    assert mod.env_number("poll_interval", 60) == 30.0
    assert mod.env_number("tick", 5) == 5.0            # unparseable -> default
    assert mod.env_number("scoped_ttl", 600) == 600.0  # unset -> default
