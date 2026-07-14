from pathlib import Path
import sqlite3

from qc_studio.config import DB_PATH, SAMPLE_TYPES
from qc_studio.schema import SCHEMA_SQL


def get_db_download_bytes(db_path=None):
    db_path = Path(db_path or DB_PATH)
    if not db_path.exists():
        return None
    return db_path.read_bytes()


def delete_database_file(db_path=None):
    db_path = Path(db_path or DB_PATH)
    if db_path.exists():
        db_path.unlink()
        return True
    return False


def ensure_uploaded_by_column(conn):
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='runs'")
    if cursor.fetchone():
        cursor.execute("PRAGMA table_info(runs)")
        columns = [row[1] for row in cursor.fetchall()]
        if "uploaded_by" not in columns:
            cursor.execute("ALTER TABLE runs ADD COLUMN uploaded_by TEXT")


def get_connection(db_path=None):
    db_path = Path(db_path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_uploaded_by_column(conn)
    return conn


def ensure_db_initialized(db_path=None):
    """Create schema and seed reference data on-demand (from first file upload)."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Create schema if not exists
    conn.executescript(SCHEMA_SQL)

    # Seed sample types if not already present
    cursor.execute("SELECT COUNT(*) FROM sample_types")
    if cursor.fetchone()[0] == 0:
        for st_item in SAMPLE_TYPES:
            conn.execute(
                "INSERT OR IGNORE INTO sample_types (type_code, description) VALUES (?, ?)",
                (st_item["type_code"], st_item["description"]),
            )

    conn.commit()
    conn.close()
