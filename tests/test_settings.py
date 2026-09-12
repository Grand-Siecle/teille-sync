import pytest
from datetime import timedelta
from pathlib import Path
from teille_sync.settings import resolve, SettingsError


def test_flag_beats_env_beats_file_beats_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.local.toml"
    cfg.write_text('nas_root = "/from/file"\nbatch_size = 3\n')
    monkeypatch.setenv("TDSYNC_NAS_ROOT", "/from/env")

    s = resolve(flags={"batch_size": 7}, config_file=cfg)

    assert s.nas_root == Path("/from/env")   # env beats file
    assert s.origin("nas_root") == "env"
    assert s.batch_size == 7                 # flag beats file
    assert s.origin("batch_size") == "flag"


def test_batch_size_rejects_zero_and_says_what_it_uses(tmp_path, monkeypatch):
    monkeypatch.setenv("TDSYNC_BATCH_SIZE", "0")
    s = resolve(flags={}, config_file=tmp_path / "absent.toml")
    assert s.batch_size == 5                 # the default
    assert "TDSYNC_BATCH_SIZE" in s.refusals[0]
    assert "5" in s.refusals[0]              # says what it used instead


def test_batch_size_as_a_flag_is_a_usage_error(tmp_path):
    with pytest.raises(SettingsError):
        resolve(flags={"batch_size": 0}, config_file=tmp_path / "absent.toml")


def test_reclaim_after_parses_hours(tmp_path, monkeypatch):
    monkeypatch.setenv("TDSYNC_RECLAIM_AFTER", "6h")
    s = resolve(flags={}, config_file=tmp_path / "absent.toml")
    assert s.reclaim_after == timedelta(hours=6)
