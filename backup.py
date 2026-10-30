"""SQLite auto-backup: DATA_DIR/backups mein daily copy, last N retained."""
import os
import sqlite3
import threading
import time
from datetime import datetime

import lead_pipeline as lp


def run_backup():
    backup_dir = os.path.join(os.getenv("DATA_DIR", "."), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dest_path = os.path.join(backup_dir, f"leads-{datetime.now():%Y%m%d-%H%M%S-%f}.db")
    src = sqlite3.connect(lp.DB_PATH)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    keep = int(os.getenv("BACKUP_KEEP", "7"))
    backups = sorted(f for f in os.listdir(backup_dir) if f.startswith("leads-") and f.endswith(".db"))
    for old in backups[:-keep]:
        os.remove(os.path.join(backup_dir, old))
    return dest_path


def _loop():
    hours = float(os.getenv("BACKUP_HOURS", "24"))
    while True:
        time.sleep(hours * 3600)
        try:
            run_backup()
        except (sqlite3.Error, OSError):
            pass  # backup best-effort hai, app nahi rokna


def start():
    t = threading.Thread(target=_loop, name="backup", daemon=True)
    t.start()