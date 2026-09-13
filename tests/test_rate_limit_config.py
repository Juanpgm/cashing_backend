"""RATE_LIMIT_ENABLED setting and its wiring into app.core.rate_limit.build_limiter."""

from app.core.config import Settings
from app.core.rate_limit import build_limiter


def test_rate_limit_enabled_defaults_to_true():
    """Every environment without an explicit override keeps limits active."""
    s = Settings(_env_file=None)
    assert s.RATE_LIMIT_ENABLED is True


def test_rate_limit_enabled_false_via_env_var(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.RATE_LIMIT_ENABLED is False


def test_rate_limit_enabled_accepts_common_boolean_spellings(monkeypatch):
    for raw, expected in [("1", True), ("0", False), ("True", True), ("False", False)]:
        monkeypatch.setenv("RATE_LIMIT_ENABLED", raw)
        assert Settings(_env_file=None).RATE_LIMIT_ENABLED is expected, raw


def test_build_limiter_enabled_true_by_default():
    limiter = build_limiter(Settings(_env_file=None))
    assert limiter.enabled is True


def test_build_limiter_enabled_false_when_settings_disable_it():
    s = Settings(_env_file=None)
    s.RATE_LIMIT_ENABLED = False
    limiter = build_limiter(s)
    assert limiter.enabled is False


def test_module_level_limiter_built_via_factory():
    """The module-level singleton `app.core.rate_limit.limiter` must be built
    via `build_limiter` (not a bare inline `Limiter(...)`), so the wiring
    tested above actually applies to it. Not asserting `.enabled` here:
    `tests/conftest.py` deliberately forces `limiter.enabled = False` for the
    whole pytest session, independent of `RATE_LIMIT_ENABLED`, by design."""
    import inspect

    import app.core.rate_limit as rate_limit_module

    source = inspect.getsource(rate_limit_module)
    assert "limiter = build_limiter(settings)" in source
