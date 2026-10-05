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


def _wait_for_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _start_pot_provider() -> subprocess.Popen | None:
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
    process = subprocess.Popen(
        command,
        cwd=str(node_modules),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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
    pot_provider = _start_pot_provider()
    try:
        command = [sys.executable, "/app/container_entrypoint.py", *sys.argv[1:]]
        os.execvp(command[0], command)
    finally:
        if pot_provider is not None and pot_provider.poll() is None:
            pot_provider.terminate()
        if health.poll() is None:
            health.terminate()


if __name__ == "__main__":
    main()
