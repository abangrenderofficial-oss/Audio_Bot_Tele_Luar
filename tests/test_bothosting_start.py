"""Fast stdlib-only checks for the isolated BotHosting startup entrypoint."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bothosting_start.py"
spec = importlib.util.spec_from_file_location("bothosting_start", SCRIPT)
bothosting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bothosting)

GOOD_ENV = {
    "BOT_TOKEN": "123456:EXAMPLE_TOKEN_DO_NOT_USE",
    "ADMIN_ID": "12345678",
    "DATABASE_URL": "postgresql://user:password@db.example.com:5432/musix",
}


def test_bothosting_accepts_valid_minimum_config():
    assert bothosting.environment_errors(GOOD_ENV) == []


def test_bothosting_rejects_ephemeral_memory_db():
    env = dict(GOOD_ENV)
    env.pop("DATABASE_URL")
    assert any("DATABASE_URL" in error for error in bothosting.environment_errors(env))


def test_bothosting_rejects_malformed_credentials():
    env = dict(GOOD_ENV)
    env["BOT_TOKEN"] = "invalid"
    env["ADMIN_ID"] = "not-a-number"
    env["DATABASE_URL"] = "sqlite:///local.db"
    errors = bothosting.environment_errors(env)
    assert any("BOT_TOKEN" in item for item in errors)
    assert any("ADMIN_ID" in item for item in errors)
    assert any("DATABASE_URL" in item for item in errors)


def test_config_check_is_offline_and_never_polls():
    env = dict(os.environ)
    env.update(GOOD_ENV)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config-check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Environment syntax OK" in result.stdout
    assert "Starting MusiX" not in result.stdout
