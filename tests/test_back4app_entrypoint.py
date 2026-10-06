from __future__ import annotations

import back4app_entrypoint


def test_low_memory_mode_skips_pot_provider(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "true")
    monkeypatch.setattr(
        back4app_entrypoint.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("POT provider must not start in low-memory mode")
        ),
    )

    assert back4app_entrypoint._start_pot_provider() is None


def test_low_memory_env_parser_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("YOUTUBE_LOW_MEMORY_MODE", "YeS")
    assert back4app_entrypoint._env_truthy("YOUTUBE_LOW_MEMORY_MODE") is True


def test_webhook_mode_disables_standalone_health_server(monkeypatch):
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.setenv(
        "TELEGRAM_WEBHOOK_BASE_URL",
        "https://abangrender-music-bot.onrender.com",
    )

    assert back4app_entrypoint._webhook_mode_enabled() is True
