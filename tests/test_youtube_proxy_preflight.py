"""No network requests: proxy preflight validation and redacted diagnostics."""
from scripts import check_youtube_proxy


def test_validate_proxy_url_accepts_authenticated_http_and_socks5h():
    assert check_youtube_proxy.validate_proxy_config("http://user:secret@proxy.example:8888")
    assert check_youtube_proxy.validate_proxy_config("socks5h://proxy.example:1080")
    assert not check_youtube_proxy.validate_proxy_config("")
    assert not check_youtube_proxy.validate_proxy_config("https://proxy.example")
    assert not check_youtube_proxy.validate_proxy_config("file:///etc/passwd")
    assert not check_youtube_proxy.validate_proxy_config("https://proxy.example:notaport")
    assert not check_youtube_proxy.validate_proxy_config("http://user:secret@proxy.example:8888 ")


def test_preflight_without_proxy_is_safe_and_offline(monkeypatch, capsys):
    monkeypatch.delenv("YTDLP_YOUTUBE_PROXY", raising=False)
    assert check_youtube_proxy.check() == 2
    assert "proxy_preflight=not_configured_or_invalid" in capsys.readouterr().out


def test_preflight_confirms_audio_formats_without_downloading(monkeypatch, capsys):
    proxy = "http://user:private-password@proxy.example:8888"
    monkeypatch.setenv("YTDLP_YOUTUBE_PROXY", proxy)
    monkeypatch.setattr(check_youtube_proxy, "build_ytdlp_youtube_options", lambda **kwargs: {"proxy": proxy, **kwargs})
    record = {}

    class StubDL:
        def __init__(self, options):
            record["options"] = options
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            record["url"] = url
            record["download"] = download
            return {"formats": [{"acodec": "opus"}]}

    monkeypatch.setattr(check_youtube_proxy, "YoutubeDL", StubDL)
    assert check_youtube_proxy.check() == 0
    assert record["download"] is False
    assert record["options"]["skip_download"] is True
    output = capsys.readouterr().out
    assert "proxy_preflight=success" in output
    assert "private-password" not in output


def test_preflight_redacts_connection_error_secrets(monkeypatch, capsys):
    proxy = "http://user:super-secret@proxy.example:8888"
    monkeypatch.setenv("YTDLP_YOUTUBE_PROXY", proxy)
    monkeypatch.setattr(check_youtube_proxy, "build_ytdlp_youtube_options", lambda **kwargs: {"proxy": proxy, **kwargs})

    class StubDL:
        def __init__(self, options):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def extract_info(self, url, download):
            raise RuntimeError("Proxy 407 for " + proxy)

    monkeypatch.setattr(check_youtube_proxy, "YoutubeDL", StubDL)
    assert check_youtube_proxy.check() == 5
    output = capsys.readouterr().out
    assert "failure_category=proxy_authentication" in output
    assert "super-secret" not in output
