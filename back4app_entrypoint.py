from __future__ import annotations

import os
import subprocess
import sys


def main() -> None:
    health = subprocess.Popen([sys.executable, "/app/health_server.py"])
    try:
        command = [sys.executable, "/app/container_entrypoint.py", *sys.argv[1:]]
        os.execvp(command[0], command)
    finally:
        if health.poll() is None:
            health.terminate()


if __name__ == "__main__":
    main()
