from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

import container_entrypoint as entrypoint


def test_prepare_youtube_cookie_env_writes_private_runtime_file(monkeypatch, tmp_path):
    runtime_file = tmp_path / ".runtime" / "cookies" / "youtube.txt"
    payload = (
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tTEST_COOKIE\ttest-value\n"
    ).encode("utf-8")

    monkeypatch.setattr(entrypoint, "RUNTIME_YOUTUBE_COOKIES_FILE", runtime_file)
    monkeypatch.setenv(
        entrypoint.YOUTUBE_COOKIES_B64_ENV,
        base64.b64encode(payload).decode("ascii"),
    )

    assert entrypoint._prepare_youtube_cookie_env(
        uid=os.getuid(),
        gid=os.getgid(),
    ) is True
    assert runtime_file.read_bytes() == payload
    assert runtime_file.stat().st_mode & 0o777 == 0o600
    assert os.environ["YTDLP_YOUTUBE_COOKIES_FILE"] == str(runtime_file)
    assert entrypoint.YOUTUBE_COOKIES_B64_ENV not in os.environ


@pytest.mark.parametrize(
    "encoded",
    [
        "not-base64!!!",
        base64.b64encode(b"").decode("ascii"),
        base64.b64encode(b"not a netscape cookie file\n").decode("ascii"),
    ],
)
def test_prepare_youtube_cookie_env_rejects_invalid_secret(monkeypatch, tmp_path, encoded):
    runtime_file = tmp_path / ".runtime" / "cookies" / "youtube.txt"
    monkeypatch.setattr(entrypoint, "RUNTIME_YOUTUBE_COOKIES_FILE", runtime_file)
    monkeypatch.setenv(entrypoint.YOUTUBE_COOKIES_B64_ENV, encoded)

    with pytest.raises(RuntimeError):
        entrypoint._prepare_youtube_cookie_env(
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert not runtime_file.exists()
