import os
import sys


def default_db_path() -> str:
    """Where the SQLite database lives when DB_PATH is not set.

    Docker and the Unraid template always pass DB_PATH explicitly, so this only
    matters for a native run outside a container: the Windows installer, or
    running from source on a dev machine.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "LoRaTheExplorer", "explorer.db")
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/LoRaTheExplorer/explorer.db"
        )
    # Native Linux and the Docker image both land here, matching the /app/data
    # volume convention baked into the Dockerfile, docker-compose.yml and the
    # Unraid template.
    return "/app/data/explorer.db"
