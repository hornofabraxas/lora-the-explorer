"""Reset LoRa the Explorer authentication. Run: python -m lora_explorer.reset_password [--data-dir /path/to/data]"""

import argparse
import os
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description="Reset LoRa the Explorer authentication")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DB_PATH", "/app/data/explorer.db"),
        help="Path to data directory or database file (default: /app/data/explorer.db)",
    )
    parser.add_argument(
        "--mode",
        choices=["password", "oidc", "all"],
        default="all",
        help="What to reset: password, oidc, or all (default: all)",
    )
    args = parser.parse_args()

    db_path = args.data_dir
    if os.path.isdir(db_path):
        db_path = os.path.join(db_path, "explorer.db")

    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        sys.exit(1)

    keys_to_delete = []
    if args.mode in ("password", "all"):
        keys_to_delete.append("password_hash")
    if args.mode in ("oidc", "all"):
        keys_to_delete.extend(["oidc_config", "oidc_sub"])

    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" * len(keys_to_delete))
        conn.execute(
            f"DELETE FROM settings WHERE key IN ({placeholders})",
            keys_to_delete,
        )
        conn.commit()
    finally:
        conn.close()

    messages = {
        "password": "Password has been reset.",
        "oidc": "OIDC configuration has been cleared.",
        "all": "All authentication has been reset.",
    }
    print(f"{messages[args.mode]} Open the game in your browser to set up again.")


if __name__ == "__main__":
    main()
