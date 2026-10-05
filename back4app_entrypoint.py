from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path


POT_HOME = Path("/opt/bgutil-pot")
POT_HOST = "127.0.0.1"
POT_PORT = 4416
WARP_HOME = Path("/app/.runtime/warp")
WGCF_BIN = Path("/usr/local/bin/wgcf")
WIREPROXY_BIN = Path("/usr/local/bin/wireproxy")
WARP_HOST = "127.0.0.1"
WARP_PORT = 1080
WARP_PROXY_URL = f"socks5://{WARP_HOST}:{WARP_PORT}"
XVFB_BIN = Path("/usr/bin/Xvfb")
XVFB_DISPLAY = (os.getenv("YTDLP_XVFB_DISPLAY") or ":99").strip() or ":99"


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _run_warp_command(command: list[str], *, timeout: float) -> bool:
    try:
        result = subprocess.run(
            command,
            cwd=str(WARP_HOME),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _prepare_warp_profile() -> Path | None:
    if not WGCF_BIN.is_file() or not WIREPROXY_BIN.is_file():
        print("[WARP] helper binaries are unavailable; continuing without WARP", flush=True)
        return None

    WARP_HOME.mkdir(parents=True, exist_ok=True)
    account_path = WARP_HOME / "wgcf-account.toml"
    profile_path = WARP_HOME / "wgcf-profile.conf"

    if not account_path.is_file():
        if not _run_warp_command(
            [str(WGCF_BIN), "register", "--accept-tos"],
            timeout=45.0,
        ):
            print("[WARP] registration failed; continuing without WARP", flush=True)
            return None

    if not profile_path.is_file():
        if not _run_warp_command(
            [str(WGCF_BIN), "generate", "--keepalive=25"],
            timeout=30.0,
        ):
            print("[WARP] profile generation failed; continuing without WARP", flush=True)
            return None

    try:
        profile = profile_path.read_text(encoding="utf-8")
        if "[Socks5]" not in profile:
            profile = profile.rstrip() + (
                f"\n\n[Socks5]\nBindAddress = {WARP_HOST}:{WARP_PORT}\n"
            )
            profile_path.write_text(profile, encoding="utf-8")
        os.chmod(profile_path, 0o600)
    except OSError:
        print("[WARP] could not prepare proxy profile; continuing without WARP", flush=True)
        return None

    return profile_path


def _verify_warp_proxy() -> bool:
    try:
        result = subprocess.run(
            [
                "curl",
                "-fsS",
                "--max-time",
                "15",
                "--socks5-hostname",
                f"{WARP_HOST}:{WARP_PORT}",
                "https://www.cloudflare.com/cdn-cgi/trace",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False
    trace = result.stdout.lower()
    return "warp=on" in trace or "warp=plus" in trace


def _start_warp_proxy() -> subprocess.Popen | None:
    if not _env_truthy("YOUTUBE_WARP_PROXY_ENABLED"):
        return None

    configured_proxy = (os.getenv("YTDLP_YOUTUBE_PROXY") or "").strip()
    if configured_proxy:
        print("[WARP] external YouTube proxy already configured; built-in WARP skipped", flush=True)
        return None

    profile_path = _prepare_warp_profile()
    if profile_path is None:
        return None

    process = subprocess.Popen(
        [str(WIREPROXY_BIN), "-c", str(profile_path), "-s"],
        cwd=str(WARP_HOME),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_port(WARP_HOST, WARP_PORT, timeout=30.0):
        if process.poll() is None:
            process.terminate()
        print("[WARP] SOCKS proxy did not become ready; continuing direct", flush=True)
        return None

    if not _verify_warp_proxy():
        if process.poll() is None:
            process.terminate()
        print("[WARP] proxy opened but WARP verification failed; continuing direct", flush=True)
        return None

    os.environ["YTDLP_YOUTUBE_PROXY"] = WARP_PROXY_URL
    print(f"[WARP] verified and ready at {WARP_PROXY_URL}", flush=True)
    return process


def _start_xvfb() -> subprocess.Popen | None:
    if not XVFB_BIN.is_file():
        print("[WPC] Xvfb is unavailable; browser PO provider may not start", flush=True)
        return None

    os.environ["DISPLAY"] = XVFB_DISPLAY
    process = subprocess.Popen(
        [
            str(XVFB_BIN),
            XVFB_DISPLAY,
            "-ac",
            "-screen",
            "0",
            "1280x720x24",
            "-nolisten",
            "tcp",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    display_number = XVFB_DISPLAY.lstrip(":").split(".", 1)[0]
    socket_path = Path(f"/tmp/.X11-unix/X{display_number}")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print("[WPC] Xvfb exited before becoming ready", flush=True)
            return None
        if socket_path.exists():
            print(f"[WPC] virtual display ready at {XVFB_DISPLAY}", flush=True)
            return process
        time.sleep(0.2)

    print("[WPC] Xvfb did not become ready; browser PO provider may fail", flush=True)
    if process.poll() is None:
        process.terminate()
    return None


def _start_pot_provider() -> subprocess.Popen | None:
    if _env_truthy("YOUTUBE_LOW_MEMORY_MODE"):
        print("[POT] skipped because YOUTUBE_LOW_MEMORY_MODE is enabled", flush=True)
        return None
    node_modules = POT_HOME / "node_modules"
    entrypoint = POT_HOME / "src" / "main.ts"
    if not node_modules.is_dir() or not entrypoint.is_file():
        print("[POT] provider files not found; continuing without HTTP provider", flush=True)
        return None

    command = [
        "/usr/local/bin/deno",
        "run",
        "--allow-env",
        "--allow-net",
        f"--allow-ffi={node_modules}",
        f"--allow-read={node_modules}",
        str(entrypoint),
        "--host",
        POT_HOST,
        "--port",
        str(POT_PORT),
    ]
    process = subprocess.Popen(command, cwd=str(node_modules))
    if _wait_for_port(POT_HOST, POT_PORT):
        print(f"[POT] HTTP provider ready at http://{POT_HOST}:{POT_PORT}", flush=True)
        return process

    return_code = process.poll()
    if return_code is None:
        print("[POT] HTTP provider did not become ready within 60s", flush=True)
        process.terminate()
    else:
        print(f"[POT] HTTP provider exited before ready: code={return_code}", flush=True)
    return None


def main() -> None:
    health = subprocess.Popen([sys.executable, "/app/health_server.py"])
    xvfb = _start_xvfb() if _env_truthy("YOUTUBE_BROWSER_WPC_ENABLED") else None
    if xvfb is None:
        print("[WPC] browser provider disabled for low-memory runtime", flush=True)
    warp_proxy = _start_warp_proxy()
    pot_provider = _start_pot_provider()
    try:
        command = [sys.executable, "/app/container_entrypoint.py", *sys.argv[1:]]
        os.execvp(command[0], command)
    finally:
        if pot_provider is not None and pot_provider.poll() is None:
            pot_provider.terminate()
        if xvfb is not None and xvfb.poll() is None:
            xvfb.terminate()
        if warp_proxy is not None and warp_proxy.poll() is None:
            warp_proxy.terminate()
        if health.poll() is None:
            health.terminate()


if __name__ == "__main__":
    main()
