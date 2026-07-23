"""
QC Studio — Unified Application
================================
Integrated platform for QC panel database management, QC data export, and dashboard visualization.

Features:
1. Panel Database: SQLite database for uploaded QC panel results
2. QC Export: Export CSV files with HQC and LQC values for all analytes
3. QC Dashboard: Interactive Levey-Jennings charts with 2SD/3SD bands

"""

import sqlite3
import re
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from datetime import datetime
import tempfile
import os
import shutil
from io import BytesIO
from urllib.parse import quote
from qc_studio.config import SAMPLE_TYPES
from qc_studio.models import SampleInfo
from qc_studio.db import (
    get_connection,
    ensure_db_initialized,
    get_db_download_bytes,
    delete_database_file,
)


# Persistent DB location:
# 1) Use QC_STUDIO_DB_PATH env var if set (RECOMMENDED: point this to a persistent mounted volume)
# 2) Else default to a repo-local file (may be ephemeral depending on hosting platform)
REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("QC_STUDIO_DB_PATH", str(REPO_ROOT / "test_panel.db"))).resolve()

SAMPLE_TYPES = [
    {"type_code": "calibrator", "description": "Calibration standards (Cal 0 through Cal F)"},
    {"type_code": "qc", "description": "Quality control samples (Low/High)"},
    {"type_code": "patient", "description": "Patient specimens"},
    {"type_code": "eqa", "description": "External quality assessment / proficiency testing"},
    {"type_code": "blank", "description": "Solvent blanks"},
    {"type_code": "process_blank", "description": "Process/extraction blanks"},
]


def ensure_persistent_db_path_ready(db_path: Path) -> None:
    """
    Ensure DB directory exists and is writable.
    Fail fast with a clear message if path is not usable.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    probe = db_path.parent / ".qc_studio_write_test"
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        probe.unlink(missing_ok=True)
    except Exception as e:
        raise RuntimeError(
            f"Database directory is not writable: {db_path.parent}\n"
            f"Set QC_STUDIO_DB_PATH to a writable persistent path.\n"
            f"Original error: {e}"
        )

def backup_database_if_due(db_path: Path, backup_dir_name: str = "backups", max_backups: int = 14) -> None:
    """
    Create at most one backup per day: backups/test_panel_YYYYMMDD.db
    Keeps only the latest `max_backups`.
    """
    if not db_path.exists():
        return

    backup_dir = db_path.parent / backup_dir_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d")
    backup_file = backup_dir / f"{db_path.stem}_{stamp}{db_path.suffix}"
    if not backup_file.exists():
        shutil.copy2(db_path, backup_file)

    # retention
    backups = sorted(
        backup_dir.glob(f"{db_path.stem}_*{db_path.suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[max_backups:]:
        old.unlink(missing_ok=True)


# ==============================================================================
# SQL SCHEMA
# ==============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    panel           INTEGER NOT NULL,
    source_filename TEXT NOT NULL UNIQUE,
    method_name     TEXT,
    data_path       TEXT,
    uploaded_by     TEXT,
    imported_at     TEXT NOT NULL DEFAULT (datetime('now')),
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS analytes (
    analyte_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    panel           INTEGER NOT NULL,
    display_order   INTEGER
);

CREATE TABLE IF NOT EXISTS sample_types (
    type_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    type_code       TEXT NOT NULL UNIQUE,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS samples (
    sample_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    data_filename   TEXT NOT NULL,
    sample_name     TEXT,
    sample_type_id  INTEGER NOT NULL REFERENCES sample_types(type_id),
    instrument_type TEXT,
    acquisition_datetime TEXT,
    autosampler_position TEXT,
    sample_group    TEXT,
    collection_date TEXT,
    patient_sequence TEXT,
    calibrator_level TEXT,
    qc_level        TEXT,
    qc_replicate    INTEGER,
    eqa_scheme      TEXT,
    eqa_year        INTEGER,
    eqa_round       INTEGER,
    eqa_sample_code TEXT,
    eqa_replicate   TEXT,
    UNIQUE(run_id, data_filename)
);

CREATE TABLE IF NOT EXISTS results (
    result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id       INTEGER NOT NULL REFERENCES samples(sample_id),
    analyte_id      INTEGER NOT NULL REFERENCES analytes(analyte_id),
    concentration   REAL,
    UNIQUE(sample_id, analyte_id)
);

CREATE TABLE IF NOT EXISTS qc_targets (
    target_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    analyte_id      INTEGER NOT NULL REFERENCES analytes(analyte_id),
    qc_level        TEXT NOT NULL,
    lot_number      TEXT,
    target_mean     REAL NOT NULL,
    target_sd       REAL NOT NULL,
    effective_from  TEXT NOT NULL,
    effective_to    TEXT,
    UNIQUE(analyte_id, qc_level, lot_number, effective_from)
);

CREATE TABLE IF NOT EXISTS eqa_targets (
    target_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    analyte_id      INTEGER NOT NULL REFERENCES analytes(analyte_id),
    scheme          TEXT NOT NULL,
    year            INTEGER NOT NULL,
    round           INTEGER NOT NULL,
    sample_code     TEXT NOT NULL,
    consensus_mean  REAL,
    consensus_sd    REAL,
    UNIQUE(analyte_id, scheme, year, round, sample_code)
);

CREATE INDEX IF NOT EXISTS idx_results_analyte ON results(analyte_id);
CREATE INDEX IF NOT EXISTS idx_results_sample ON results(sample_id);
CREATE INDEX IF NOT EXISTS idx_samples_type ON samples(sample_type_id);
CREATE INDEX IF NOT EXISTS idx_samples_run ON samples(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);
CREATE INDEX IF NOT EXISTS idx_samples_qc ON samples(qc_level) WHERE qc_level IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_samples_eqa ON samples(eqa_scheme, eqa_year, eqa_round, eqa_sample_code)
    WHERE eqa_scheme IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_qc_targets_lookup ON qc_targets(analyte_id, qc_level, effective_from);
"""

# ==============================================================================
# SAMPLE CLASSIFIER
# ==============================================================================

# NOTE:
# SampleInfo is imported from qc_studio.models.
# Keep a single source of truth and avoid redefining it here.

 def main():
    # Validate DB path early so the app fails with actionable guidance.
    ensure_persistent_db_path_ready(DB_PATH)
    # Optional safety net: one snapshot backup per day.
    backup_database_if_due(DB_PATH)

     st.set_page_config(page_title="QC Studio", layout="wide")
     st.title("🧪 QC Studio")
     st.markdown("Integrated QC panel database, QC export, and dashboard platform")
     st.sidebar.caption(f"DB: {DB_PATH}")
    if "QC_STUDIO_DB_PATH" not in os.environ:
        st.sidebar.warning("Using fallback local DB path (may be ephemeral on this host).")



def classify_sample(data_filename: str) -> SampleInfo:
    """Parse data filename into structured sample information."""
    # Robust normalize: trim, remove optional _P1/_P2 and .d (case-insensitive)
    name = str(data_filename).strip()
    base = re.sub(r"(_P[12])?\.d$", "", name, flags=re.IGNORECASE)

    cal_match = re.match(r"^Cal\s+([0A-F])$", base, flags=re.IGNORECASE)
    if cal_match:
        return SampleInfo(
            data_filename=data_filename,
            sample_type="calibrator",
            calibrator_level=cal_match.group(1).upper()
        )

    # ✅ robust QC parsing (handles QC_Low1, QC_High2, optional .d/_P1 in original name)
    qc_match = re.match(r"^QC_(Low|High)\s*(\d+)$", base, flags=re.IGNORECASE)
    if qc_match:
        return SampleInfo(
            data_filename=data_filename,
            sample_type="qc",
            qc_level=qc_match.group(1).capitalize(),   # "Low" / "High"
            qc_replicate=int(qc_match.group(2))
        )

    if re.match(r"^Blank\d*$", base, flags=re.IGNORECASE):
        return SampleInfo(data_filename=data_filename, sample_type="blank")

    if re.match(r"^(PBlank|PB)\d*$", base, flags=re.IGNORECASE):
        return SampleInfo(data_filename=data_filename, sample_type="process_blank")

    eqa_match = re.match(r"^([A-Za-z]+)(\d{4})_(\d)([A-Z])-([a-z])$", base)
    if eqa_match:
        return SampleInfo(
            data_filename=data_filename, sample_type="eqa",
            eqa_scheme=eqa_match.group(1), eqa_year=int(eqa_match.group(2)),
            eqa_round=int(eqa_match.group(3)), eqa_sample_code=eqa_match.group(4),
            eqa_replicate=eqa_match.group(5)
        )

    eqa_special = re.match(r"^([A-Za-z]+)(\d{4})_(\d)([A-Z])_(.+)$", base)
    if eqa_special:
        return SampleInfo(
            data_filename=data_filename, sample_type="eqa",
            eqa_scheme=eqa_special.group(1), eqa_year=int(eqa_special.group(2)),
            eqa_round=int(eqa_special.group(3)), eqa_sample_code=eqa_special.group(4),
            eqa_replicate=eqa_special.group(5)
        )

    eqa_new_rep = re.match(r"^([A-Za-z]+)(\d{4})_(\d)([A-Z])([a-z])$", base)
    if eqa_new_rep:
        return SampleInfo(
            data_filename=data_filename, sample_type="eqa",
            eqa_scheme=eqa_new_rep.group(1), eqa_year=int(eqa_new_rep.group(2)),
            eqa_round=int(eqa_new_rep.group(3)), eqa_sample_code=eqa_new_rep.group(4),
            eqa_replicate=eqa_new_rep.group(5)
        )

    eqa_new = re.match(r"^([A-Za-z]+)(\d{4})_(\d)([A-Z])$", base)
    if eqa_new:
        return SampleInfo(
            data_filename=data_filename, sample_type="eqa",
            eqa_scheme=eqa_new.group(1), eqa_year=int(eqa_new.group(2)),
            eqa_round=int(eqa_new.group(3)), eqa_sample_code=eqa_new.group(4), eqa_replicate=None
        )

    patient_match = re.match(r"^(\d{8})_(\w+)$", base)
    if patient_match:
        date_str = patient_match.group(1)
        formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        return SampleInfo(
            data_filename=data_filename, sample_type="patient",
            collection_date=formatted_date, patient_sequence=patient_match.group(2)
        )

    return SampleInfo(data_filename=data_filename, sample_type="patient")


def classify_from_instrument_type(instrument_type: str, level: str, data_filename: str) -> SampleInfo:
    """Use instrument-assigned Type column to classify."""
    itype = str(instrument_type).strip() if pd.notna(instrument_type) else ""
    lvl = str(level).strip() if pd.notna(level) else ""

    if itype == "DoubleBlank":
        return SampleInfo(data_filename=data_filename, sample_type="blank")

    if itype == "Blank":
        return SampleInfo(data_filename=data_filename, sample_type="process_blank")

    if itype == "MatrixBlank":
        return SampleInfo(data_filename=data_filename, sample_type="calibrator", calibrator_level="0")

    if itype == "Cal":
        return SampleInfo(data_filename=data_filename, sample_type="calibrator", calibrator_level=lvl)

    if itype == "QC":
        # ✅ same robust parsing as classify_sample
        name = str(data_filename).strip()
        base = re.sub(r"(_P[12])?\.d$", "", name, flags=re.IGNORECASE)
        qc_match = re.match(r"^QC_(Low|High)\s*(\d+)$", base, flags=re.IGNORECASE)

        qc_level = normalize_qc_level(lvl) if lvl else None
        qc_replicate = int(qc_match.group(2)) if qc_match else 1
        if qc_match and not qc_level:
            qc_level = qc_match.group(1).capitalize()

        return SampleInfo(
            data_filename=data_filename,
            sample_type="qc",
            qc_level=qc_level,
            qc_replicate=qc_replicate
        )

    return classify_sample(data_filename)


# ==============================================================================
# FORMAT DETECTION AND FILENAME PARSING
# ==============================================================================
# ==============================================================================
# CSV IMPORTER — NEW FORMAT ONLY (Type/Level filtered)
# ==============================================================================

def _normalize_colname(c: str) -> str:
    c = str(c).strip().lower()
    c = re.sub(r"[^a-z0-9]+", " ", c).strip()
    return c


def _find_column_fuzzy(df_cols, aliases):
    norm_to_real = {_normalize_colname(c): c for c in df_cols}
    # exact normalized match first
    for a in aliases:
        a_norm = _normalize_colname(a)
        if a_norm in norm_to_real:
            return norm_to_real[a_norm]
    # contains match fallback
    for c in df_cols:
        c_norm = _normalize_colname(c)
        for a in aliases:
            a_norm = _normalize_colname(a)
            if a_norm in c_norm or c_norm in a_norm:
                return c
    return None


def _require_columns(df, required_aliases_map):
    missing = []
    resolved = {}
    for logical_name, aliases in required_aliases_map.items():
        col = _find_column_fuzzy(df.columns, aliases)
        if col is None:
            missing.append(f"{logical_name} ({', '.join(aliases)})")
        else:
            resolved[logical_name] = col
    if missing:
        raise ValueError(
            "CSV is missing required new-format columns:\n- " + "\n- ".join(missing)
        )
    return resolved


def _extract_new_csv_structure(csv_path: str):
    """
    Supports both common 'new' layouts:
    1) Two-row header layout (row0 analyte '... Results', row1 metadata labels)
    2) Single-row flat table with explicit columns.
    Returns:
      df_meta, analyte_columns (list of tuples: real_col_idx, analyte_name), col_map, first_data_path
    """
    # Try two-row style first
    df_raw = pd.read_csv(csv_path, header=None)
    if len(df_raw) >= 2:
        row0 = [str(v).strip() if pd.notna(v) else "" for v in df_raw.iloc[0].tolist()]
        row1 = [str(v).strip() if pd.notna(v) else "" for v in df_raw.iloc[1].tolist()]

        analyte_cols = []
        analyte_start_idx = None
        for i, h in enumerate(row0):
            if "results" in h.lower():
                if analyte_start_idx is None:
                    analyte_start_idx = i
                analyte_name = normalize_analyte_name(re.sub(r"\s*results\s*$", "", h, flags=re.IGNORECASE).strip())
                analyte_cols.append((i, analyte_name))

        if analyte_cols:
            # Metadata region is row1 up to analyte_start_idx
            meta_names = row1[:analyte_start_idx]
            # Build a data frame with canonical metadata names + analyte numeric columns
            records = []
            for r in range(2, len(df_raw)):
                row = df_raw.iloc[r].tolist()
                records.append(row)
            df_data = pd.DataFrame(records)

            # Resolve meta columns by position
            meta_df = pd.DataFrame()
            for i, name in enumerate(meta_names):
                meta_df[name] = df_data.iloc[:, i] if i < df_data.shape[1] else np.nan

            # Add analyte columns by canonical names
            for col_idx, analyte_name in analyte_cols:
                if col_idx < df_data.shape[1]:
                    meta_df[f"__ANALYTE__::{analyte_name}"] = df_data.iloc[:, col_idx]
                else:
                    meta_df[f"__ANALYTE__::{analyte_name}"] = np.nan

            # Required metadata columns (fuzzy)
            required = _require_columns(
                meta_df,
                {
                    "type": ["Type"],
                    "level": ["Level"],
                    "data_file": ["Data File", "DataFile", "File", "Data Filename"],
                    "data_path": ["Data Path", "DataPath", "Path"],
                    "acq_datetime": ["Acq. Date-Time", "Acq Date-Time", "Acq Date Time", "Acquisition Date-Time", "Acquisition Datetime"],
                },
            )

            first_data_path = None
            if required["data_path"] in meta_df.columns and len(meta_df) > 0:
                first_val = meta_df[required["data_path"]].iloc[0]
                first_data_path = str(first_val).strip() if pd.notna(first_val) and str(first_val).strip() else None

            return (
                meta_df,
                [(c, c.replace("__ANALYTE__::", "")) for c in meta_df.columns if str(c).startswith("__ANALYTE__::")],
                required,
                first_data_path,
            )

    # Fallback: single header CSV
    df = pd.read_csv(csv_path, header=0)
    required = _require_columns(
        df,
        {
            "type": ["Type"],
            "level": ["Level"],
            "data_file": ["Data File", "DataFile", "File", "Data Filename"],
            "data_path": ["Data Path", "DataPath", "Path"],
            "acq_datetime": ["Acq. Date-Time", "Acq Date-Time", "Acq Date Time", "Acquisition Date-Time", "Acquisition Datetime"],
        },
    )

    analyte_cols = []
    for c in df.columns:
        if "results" in str(c).lower():
            analyte_name = normalize_analyte_name(re.sub(r"\s*results\s*$", "", str(c), flags=re.IGNORECASE).strip())
            analyte_cols.append((c, analyte_name))

    if not analyte_cols:
        raise ValueError("CSV must contain analyte result columns (e.g., '<Analyte> Results').")

    first_data_path = None
    if len(df) > 0:
        v = df[required["data_path"]].iloc[0]
        first_data_path = str(v).strip() if pd.notna(v) and str(v).strip() else None

    return df, analyte_cols, required, first_data_path

def parse_filename_new(filename):
    """
    Parse source filename into metadata expected by import_csv_new.
    Returns: dict with source_filename, run_date, panel, method_name
    """
    source_filename = Path(filename).name if filename else "uploaded.csv"
    run_date = extract_date_from_filename(source_filename) or datetime.today().strftime("%Y-%m-%d")

    # Optional panel extraction from filename like "...panel2..." or "...panel_2..."
    panel = 1
    m = re.search(r"panel[_\s-]?(\d+)", source_filename, flags=re.IGNORECASE)
    if m:
        try:
            panel = int(m.group(1))
        except Exception:
            panel = 1

    return {
        "source_filename": source_filename,
        "run_date": run_date,
        "panel": panel,
        "method_name": None,
    }

def import_csv_new(csv_path: str, db_path=None, uploaded_by=None, original_filename=None):
    """
    Import new-format CSV ONLY.
    Strict QC filter:
      - Type must be QC
      - Level must normalize to High/Low
    Requires Acq Date-Time-like column.
    """
    ensure_db_initialized(db_path)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    uploaded_by = str(uploaded_by).strip().upper() if uploaded_by else None
    meta = parse_filename_new(original_filename or csv_path)

    cursor.execute("SELECT run_id FROM runs WHERE source_filename = ?", (meta["source_filename"],))
    if cursor.fetchone():
        conn.close()
        return f"Already imported: {meta['source_filename']}"

    df_data, analyte_cols, col_map, first_data_path = _extract_new_csv_structure(csv_path)

    # Determine run_date from first valid acq datetime (fallback filename date)
    run_date = None
    for v in df_data[col_map["acq_datetime"]].tolist():
        d = parse_date_value(v)
        if d:
            run_date = d
            break
    if run_date is None:
        run_date = meta["run_date"]

    cursor.execute(
        "INSERT INTO runs (run_date, panel, source_filename, method_name, data_path, uploaded_by) VALUES (?, ?, ?, ?, ?, ?)",
        (run_date, meta["panel"], meta["source_filename"], meta["method_name"], first_data_path, uploaded_by)
    )
    run_id = cursor.lastrowid

    # analytes
    analyte_id_map = {}
    for i, (_, analyte_name) in enumerate(analyte_cols):
        conn.execute(
            "INSERT OR IGNORE INTO analytes (name, panel, display_order) VALUES (?, ?, ?)",
            (analyte_name, meta["panel"], i + 1)
        )
        row = conn.execute(
            "SELECT analyte_id FROM analytes WHERE lower(name)=lower(?) ORDER BY analyte_id LIMIT 1",
            (analyte_name,)
        ).fetchone()
        if row:
            analyte_id_map[analyte_name] = row[0]

    type_map = dict(conn.execute("SELECT type_code, type_id FROM sample_types").fetchall())
    qc_type_id = type_map["qc"]

    imported_count = 0
    skipped_non_qc = 0

    for _, row in df_data.iterrows():
        type_val = str(row[col_map["type"]]).strip() if pd.notna(row[col_map["type"]]) else ""
        if type_val.lower() != "qc":
            skipped_non_qc += 1
            continue

        qc_level = normalize_qc_level(row[col_map["level"]])
        if qc_level not in {"High", "Low"}:
            continue

        data_filename = str(row[col_map["data_file"]]).strip() if pd.notna(row[col_map["data_file"]]) else None
        if not data_filename or data_filename.lower() == "nan":
            continue

        acq_raw = row[col_map["acq_datetime"]]
        acq_datetime = str(acq_raw).strip() if pd.notna(acq_raw) else None
        collection_date = parse_date_value(acq_raw) or run_date

        # replicate parsed from filename if possible
        info = classify_sample(data_filename)
        qc_replicate = info.qc_replicate if info.qc_replicate else 1

        cursor.execute(
            """
            INSERT INTO samples (
                run_id, data_filename, sample_name, sample_type_id, instrument_type,
                acquisition_datetime, autosampler_position, sample_group,
                collection_date, patient_sequence,
                calibrator_level, qc_level, qc_replicate,
                eqa_scheme, eqa_year, eqa_round, eqa_sample_code, eqa_replicate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                data_filename,
                None,
                qc_type_id,
                "QC",
                acq_datetime,
                None,
                None,
                collection_date,
                None,
                None,
                qc_level,
                qc_replicate,
                None, None, None, None, None
            ),
        )
        sample_id = cursor.lastrowid

        # insert analyte results
        for real_col, analyte_name in analyte_cols:
            raw_value = row[real_col] if real_col in row.index else None
            concentration = None
            if pd.notna(raw_value) and str(raw_value).strip() != "":
                try:
                    concentration = float(raw_value)
                except Exception:
                    concentration = None

            cursor.execute(
                "INSERT INTO results (sample_id, analyte_id, concentration) VALUES (?, ?, ?)",
                (sample_id, analyte_id_map[analyte_name], concentration),
            )

        imported_count += 1

    conn.commit()
    conn.close()
    return (
        f"Imported {imported_count} QC samples from {meta['source_filename']}"
        + (f" (skipped {skipped_non_qc} non-QC rows)" if skipped_non_qc else "")
    )


def import_csv(csv_path: str, db_path=None, uploaded_by=None, original_filename=None):
    """
    New behavior: only accept new-format CSV with Type/Level/Acq Date-Time style columns.
    """
    return import_csv_new(csv_path, db_path=db_path, uploaded_by=uploaded_by, original_filename=original_filename)



def normalize_qc_level(value):
    if pd.isna(value):
        return None

    label = str(value).strip().lower()
    if not label:
        return None

    if label in {"hqc", "high", "high qc", "high control", "high_level", "high level", "highqc"}:
        return "High"
    if label in {"lqc", "low", "low qc", "low control", "low_level", "low level", "lowqc"}:
        return "Low"

    if "high" in label and "low" not in label:
        return "High"
    if "low" in label and "high" not in label:
        return "Low"

    if label == "h":
        return "High"
    if label == "l":
        return "Low"

    return None


def find_column(df, tokens):
    for col in df.columns:
        lower = str(col).strip().lower()
        for token in tokens:
            if token in lower:
                return col
    return None


def parse_date_value(value):
    if pd.isna(value):
        return None

    try:
        dt = pd.to_datetime(str(value), dayfirst=False, errors='coerce')
    except Exception:
        return None

    if pd.isna(dt):
        return None
    return dt.strftime("%Y-%m-%d")


def extract_date_from_filename(filename):
    if not filename:
        return None

    text = str(filename)
    match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)

    match = re.search(r"(\d{8})", text)
    if match:
        date_str = match.group(1)
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    return None


def normalize_analyte_name(name):
    """Return a canonical analyte display name without panel-specific alias assumptions."""
    if pd.isna(name):
        return ""
    raw = str(name).strip()
    # Normalize repeated whitespace but keep original analyte identity unchanged.
    return re.sub(r"\s+", " ", raw)


# Generic sheet names that should not be treated as analyte names
_EXCLUDED_SHEET_NAMES = {
    "sheet1", "sheet2", "sheet3", "sheet4", "sheet5",
    "summary", "data", "results", "targets", "overview",
    "template", "index", "contents", "",
}


def _normalize_sheet_key(sheet_name):
    return re.sub(r"\s+", " ", str(sheet_name).strip().lower())


_NON_ANALYTE_SHEET_KEYS = {
    "index",
    "tecan calibrants",
    "tecan qc concentrations",
    "summary",
    "overview",
    "targets",
    "data",
    "results",
    "contents",
    "template",
    "",
}


def is_non_analyte_sheet(sheet_name):
    return _normalize_sheet_key(sheet_name) in _NON_ANALYTE_SHEET_KEYS


def find_analyte_name_in_workbook(sheet_name, sample_row=None):
    """Derive the analyte name from a sheet name or the first row of data.
    No hardcoded analyte list — names come entirely from the uploaded file."""
    cleaned = str(sheet_name).strip()
    if cleaned and cleaned.lower() not in _EXCLUDED_SHEET_NAMES:
        return cleaned

    # Fall back: look for a non-numeric, non-empty cell in sample_row
    if sample_row is not None:
        for value in sample_row:
            if pd.notna(value):
                candidate = str(value).strip()
                if candidate and candidate.lower() not in _EXCLUDED_SHEET_NAMES:
                    try:
                        float(candidate)  # skip numeric values
                    except ValueError:
                        return candidate

    return None


def find_qc_summary_header_row(df):
    for row_idx in range(min(len(df) - 1, 60)):
        row_vec = df.iloc[row_idx].tolist()
        row_strs = [str(v).strip().lower() if pd.notna(v) else "" for v in row_vec]
        has_qc = any("hqc" in s for s in row_strs) and any("lqc" in s for s in row_strs)
        has_stats = any("qc mean" in s for s in row_strs) and (
            any("+2sd" in s for s in row_strs) or any("-2sd" in s for s in row_strs)
        )
        if has_qc and has_stats:
            return row_idx, row_strs
    return None, None


def find_header_index(row_strs, tokens, start=0, end=None):
    end = end if end is not None else len(row_strs)
    for idx in range(start, end):
        cell = row_strs[idx]
        if not cell:
            continue
        if all(token in cell for token in tokens):
            return idx
    return None


def calculate_sd(mean, sd=None, upper2=None, lower2=None, upper3=None, lower3=None, cv=None):
    if mean is None or pd.isna(mean):
        return None

    if sd is not None and not pd.isna(sd):
        try:
            return abs(float(sd))
        except Exception:
            pass

    if upper2 is not None and not pd.isna(upper2):
        try:
            return abs(float(upper2) - float(mean)) / 2
        except Exception:
            pass

    if lower2 is not None and not pd.isna(lower2):
        try:
            return abs(float(mean) - float(lower2)) / 2
        except Exception:
            pass

    if upper3 is not None and not pd.isna(upper3):
        try:
            return abs(float(upper3) - float(mean)) / 3
        except Exception:
            pass

    if lower3 is not None and not pd.isna(lower3):
        try:
            return abs(float(mean) - float(lower3)) / 3
        except Exception:
            pass

    if cv is not None and not pd.isna(cv):
        try:
            return float(mean) * float(cv) / 100.0
        except Exception:
            pass

    return None


def parse_qc_targets_from_sheet(sheet_name, df, file_date):
    analyte = find_analyte_name_in_workbook(sheet_name, sample_row=df.iloc[0].tolist() if len(df) > 0 else None)
    if analyte is None:
        return []
    analyte = normalize_analyte_name(analyte)

    def cell_text(v):
        return str(v).strip().lower() if pd.notna(v) else ""

    def label_contains(label, *tokens):
        return all(t in label for t in tokens)

    def collect_global_level_anchors():
        anchors = {"hqc": [], "lqc": []}
        for r in range(min(len(df), 80)):
            row_vals = [cell_text(v) for v in df.iloc[r].tolist()]
            for c_idx, lbl in enumerate(row_vals):
                if not lbl:
                    continue
                if lbl == "hqc" or "hqc %cv" in lbl:
                    anchors["hqc"].append(c_idx)
                if lbl == "lqc" or "lqc %cv" in lbl:
                    anchors["lqc"].append(c_idx)
        anchors["hqc"] = sorted(set(anchors["hqc"]))
        anchors["lqc"] = sorted(set(anchors["lqc"]))
        return anchors

    global_level_anchors = collect_global_level_anchors()

    def collect_stat_candidates(header_row):
        cols = {
            "mean": [],
            "sd": [],
            "cv": [],
            "plus2": [],
            "minus2": [],
            "plus3": [],
            "minus3": [],
        }
        for idx, raw in enumerate(header_row):
            label = cell_text(raw)
            if not label:
                continue
            if "qc mean" in label or label == "mean":
                cols["mean"].append(idx)
            if "+2sd" in label or label_contains(label, "2sd", "ucl"):
                cols["plus2"].append(idx)
            if "-2sd" in label or label_contains(label, "2sd", "lcl"):
                cols["minus2"].append(idx)
            if "+3sd" in label or label_contains(label, "3sd", "ucl"):
                cols["plus3"].append(idx)
            if "-3sd" in label or label_contains(label, "3sd", "lcl"):
                cols["minus3"].append(idx)
            if "%cv" in label:
                cols["cv"].append(idx)
            if "sd" in label and all(k not in label for k in ["+2sd", "-2sd", "+3sd", "-3sd"]):
                cols["sd"].append(idx)
        return cols

    def choose_col_for_section(candidates, anchor_col, left_bound, right_bound):
        if not candidates:
            return None
        in_section = [c for c in candidates if left_bound <= c <= right_bound]
        if not in_section:
            return None
        return min(in_section, key=lambda c: abs(c - anchor_col))

    def select_stat_cols_for_anchor(candidate_cols, anchor_col, all_level_anchor_cols):
        left_candidates = [c for c in all_level_anchor_cols if c < anchor_col]
        right_candidates = [c for c in all_level_anchor_cols if c > anchor_col]
        left_bound = (max(left_candidates) + 1) if left_candidates else 0
        right_bound = (min(right_candidates) - 1) if right_candidates else 10**9
        return {
            key: choose_col_for_section(vals, anchor_col, left_bound, right_bound)
            for key, vals in candidate_cols.items()
        }

    def first_numeric_from_row(row, idx):
        if idx is None or idx >= len(row):
            return None
        try:
            val = pd.to_numeric(pd.Series([row[idx]]), errors="coerce").iloc[0]
            return None if pd.isna(val) else float(val)
        except Exception:
            return None

    def pick_value_row(start_idx, stat_cols):
        # First row under header that contains a numeric QC mean, else first row with >=2 numeric stat fields.
        for r in range(start_idx + 1, min(start_idx + 6, len(df))):
            row = df.iloc[r].tolist()
            mean_val = first_numeric_from_row(row, stat_cols.get("mean"))
            if mean_val is not None:
                return row
        for r in range(start_idx + 1, min(start_idx + 6, len(df))):
            row = df.iloc[r].tolist()
            num_count = 0
            for key in ["sd", "plus2", "minus2", "plus3", "minus3", "cv"]:
                if first_numeric_from_row(row, stat_cols.get(key)) is not None:
                    num_count += 1
            if num_count >= 2:
                return row
        return None

    def parse_level_block(level_key, db_qc_level):
        for r in range(min(len(df), 120)):
            row_labels = [cell_text(v) for v in df.iloc[r].tolist()]
            level_anchor_cols = [
                idx for idx, lbl in enumerate(row_labels)
                if level_key == lbl or level_key in lbl
            ]
            if not level_anchor_cols:
                continue

            # Prefer global anchors (banner/HQC%CV/LQC%CV rows) to define left/right section bounds.
            all_anchor_cols = sorted(set(global_level_anchors["hqc"] + global_level_anchors["lqc"]))
            if level_key in global_level_anchors and global_level_anchors[level_key]:
                level_anchor_cols = sorted(set(level_anchor_cols + global_level_anchors[level_key]))

            if len(global_level_anchors["hqc"]) and len(global_level_anchors["lqc"]):
                # Hard split between HQC and LQC zones to avoid cross-binding columns.
                split = (max(global_level_anchors["hqc"]) + min(global_level_anchors["lqc"])) / 2.0
                if level_key == "hqc":
                    level_anchor_cols = [c for c in level_anchor_cols if c <= split] or level_anchor_cols
                    all_anchor_cols = [c for c in all_anchor_cols if c <= split] + [int(split)]
                else:
                    level_anchor_cols = [c for c in level_anchor_cols if c >= split] or level_anchor_cols
                    all_anchor_cols = [int(split)] + [c for c in all_anchor_cols if c >= split]

            if not all_anchor_cols:
                all_anchor_cols = level_anchor_cols

            # Header row is usually directly below level label, but scan a few rows down for flexibility.
            for h in range(r + 1, min(r + 8, len(df))):
                header = df.iloc[h].tolist()
                header_labels = [cell_text(v) for v in header]
                candidate_cols = collect_stat_candidates(header)
                if not candidate_cols["mean"]:
                    continue

                # Determine local HQC/LQC block boundaries from this header row.
                local_markers = []
                for idx, lbl in enumerate(header_labels):
                    if lbl == "hqc" or "hqc %cv" in lbl:
                        local_markers.append((idx, "hqc"))
                    if lbl == "lqc" or "lqc %cv" in lbl:
                        local_markers.append((idx, "lqc"))
                local_markers = sorted(local_markers, key=lambda x: x[0])

                for anchor_col in level_anchor_cols:
                    split = None
                    if len(global_level_anchors["hqc"]) and len(global_level_anchors["lqc"]):
                        split = (max(global_level_anchors["hqc"]) + min(global_level_anchors["lqc"])) / 2.0

                    # Hard filter by level side first; prevents LQC from using HQC +2/+3 SD columns.
                    filtered_candidates = candidate_cols
                    if split is not None:
                        if level_key == "hqc":
                            filtered_candidates = {
                                k: [c for c in vals if c <= split]
                                for k, vals in candidate_cols.items()
                            }
                        else:
                            filtered_candidates = {
                                k: [c for c in vals if c >= split]
                                for k, vals in candidate_cols.items()
                            }

                    # Prefer strict bounds from local markers so LQC cannot read HQC stats and vice versa.
                    if local_markers:
                        same_level = [m for m in local_markers if m[1] == level_key]
                        if same_level:
                            marker_col = min(same_level, key=lambda x: abs(x[0] - anchor_col))[0]
                        else:
                            marker_col = anchor_col

                        right_markers = [m[0] for m in local_markers if m[0] > marker_col]
                        left_markers = [m[0] for m in local_markers if m[0] < marker_col]
                        left_bound = marker_col
                        right_bound = (min(right_markers) - 1) if right_markers else (len(header) - 1)
                        # If a previous marker exists, keep within current block span.
                        if left_markers:
                            left_bound = max(left_bound, max(left_markers) + 1)

                        stat_cols = {
                            key: choose_col_for_section(vals, anchor_col, left_bound, right_bound)
                            for key, vals in filtered_candidates.items()
                        }
                    else:
                        stat_cols = select_stat_cols_for_anchor(filtered_candidates, anchor_col, all_anchor_cols)

                    if stat_cols["mean"] is None:
                        continue
                    if all(stat_cols[k] is None for k in ["sd", "plus2", "minus2", "plus3", "minus3", "cv"]):
                        continue

                    value_row = pick_value_row(h, stat_cols)
                    if value_row is None:
                        continue

                    mean_val = first_numeric_from_row(value_row, stat_cols["mean"])
                    sd_val = calculate_sd(
                        mean_val,
                        sd=first_numeric_from_row(value_row, stat_cols["sd"]),
                        upper2=first_numeric_from_row(value_row, stat_cols["plus2"]),
                        lower2=first_numeric_from_row(value_row, stat_cols["minus2"]),
                        upper3=first_numeric_from_row(value_row, stat_cols["plus3"]),
                        lower3=first_numeric_from_row(value_row, stat_cols["minus3"]),
                        cv=first_numeric_from_row(value_row, stat_cols["cv"]),
                    )
                    if mean_val is None or sd_val is None or pd.isna(mean_val) or pd.isna(sd_val) or float(sd_val) == 0:
                        continue

                    return {
                        "analyte": analyte,
                        "qc_level": db_qc_level,
                        "target_mean": float(mean_val),
                        "target_sd": float(abs(sd_val)),
                        "effective_from": file_date,
                    }
        return None

    targets = []
    hqc_target = parse_level_block("hqc", "High")
    lqc_target = parse_level_block("lqc", "Low")
    if hqc_target:
        targets.append(hqc_target)
    if lqc_target:
        targets.append(lqc_target)

    # Backward compatibility with older single-header layouts where HQC/LQC are on one row.
    if targets:
        return targets

    header_row_idx, header_row = find_qc_summary_header_row(df)
    if header_row_idx is None or header_row_idx + 1 >= len(df):
        return []

    value_row = df.iloc[header_row_idx + 1].tolist()
    hqc_start = next((i for i, cell in enumerate(header_row) if "hqc" in cell), None)
    lqc_start = next((i for i, cell in enumerate(header_row) if "lqc" in cell), None)
    if hqc_start is None or lqc_start is None:
        return []

    hqc_end = lqc_start
    lqc_end = len(header_row)

    hqc_mean_col = find_header_index(header_row, ["qc mean"], start=hqc_start, end=hqc_end)
    hqc_plus2_col = find_header_index(header_row, ["+2sd"], start=hqc_start, end=hqc_end)
    hqc_minus2_col = find_header_index(header_row, ["-2sd"], start=hqc_start, end=hqc_end)
    hqc_plus3_col = find_header_index(header_row, ["+3sd"], start=hqc_start, end=hqc_end)
    hqc_minus3_col = find_header_index(header_row, ["-3sd"], start=hqc_start, end=hqc_end)
    hqc_cv_col = find_header_index(header_row, ["%cv"], start=hqc_start, end=hqc_end)

    lqc_mean_col = find_header_index(header_row, ["qc mean"], start=lqc_start, end=lqc_end)
    lqc_plus2_col = find_header_index(header_row, ["+2sd"], start=lqc_start, end=lqc_end)
    lqc_minus2_col = find_header_index(header_row, ["-2sd"], start=lqc_start, end=lqc_end)
    lqc_plus3_col = find_header_index(header_row, ["+3sd"], start=lqc_start, end=lqc_end)
    lqc_minus3_col = find_header_index(header_row, ["-3sd"], start=lqc_start, end=lqc_end)
    lqc_cv_col = find_header_index(header_row, ["%cv"], start=lqc_start, end=lqc_end)

    for level, mean_col, p2_col, m2_col, p3_col, m3_col, cv_col in [
        ("High", hqc_mean_col, hqc_plus2_col, hqc_minus2_col, hqc_plus3_col, hqc_minus3_col, hqc_cv_col),
        ("Low", lqc_mean_col, lqc_plus2_col, lqc_minus2_col, lqc_plus3_col, lqc_minus3_col, lqc_cv_col),
    ]:
        mean_val = value_row[mean_col] if mean_col is not None else None
        sd_val = calculate_sd(
            mean_val,
            upper2=value_row[p2_col] if p2_col is not None else None,
            lower2=value_row[m2_col] if m2_col is not None else None,
            upper3=value_row[p3_col] if p3_col is not None else None,
            lower3=value_row[m3_col] if m3_col is not None else None,
            cv=value_row[cv_col] if cv_col is not None else None,
        )
        if mean_val is not None and sd_val is not None and not pd.isna(mean_val) and not pd.isna(sd_val) and float(sd_val) != 0:
            targets.append({
                "analyte": analyte,
                "qc_level": level,
                "target_mean": float(mean_val),
                "target_sd": float(abs(sd_val)),
                "effective_from": file_date,
            })

    return targets


def find_qc_run_header_row(df):
    for row_idx in range(min(len(df), 80)):
        row_vec = df.iloc[row_idx].tolist()
        row_strs = [str(v).strip().lower() if pd.notna(v) else "" for v in row_vec]
        if "run" in row_strs and "date" in row_strs and row_strs.count("result") >= 2:
            return row_idx, row_strs
    return None, None


def parse_qc_run_rows_from_sheet(sheet_name, df):
    analyte = find_analyte_name_in_workbook(sheet_name, sample_row=df.iloc[0].tolist() if len(df) > 0 else None)
    if analyte is None:
        return []
    analyte = normalize_analyte_name(analyte)

    header_row_idx, header_row = find_qc_run_header_row(df)
    if header_row_idx is None:
        return []

    date_col = find_header_index(header_row, ["date"])
    run_col = find_header_index(header_row, ["run"])
    result_cols = [i for i, value in enumerate(header_row) if value == "result"]
    if len(result_cols) < 2:
        return []

    def detect_result_level_cols():
        # Try to map each RESULT column to HQC/LQC by nearest section marker.
        section_markers = []
        scan_start = max(0, header_row_idx - 8)
        for r in range(scan_start, header_row_idx + 1):
            row_vals = [str(v).strip().lower() if pd.notna(v) else "" for v in df.iloc[r].tolist()]
            for c_idx, cell in enumerate(row_vals):
                if cell == "hqc" or "hqc" in cell:
                    section_markers.append((c_idx, "High"))
                if cell == "lqc" or "lqc" in cell:
                    section_markers.append((c_idx, "Low"))

        if not section_markers:
            return result_cols[0], result_cols[1]

        def nearest_level(col_idx):
            left = [m for m in section_markers if m[0] <= col_idx]
            if left:
                return max(left, key=lambda x: x[0])[1]
            return min(section_markers, key=lambda x: abs(x[0] - col_idx))[1]

        high_cols = [c for c in result_cols if nearest_level(c) == "High"]
        low_cols = [c for c in result_cols if nearest_level(c) == "Low"]

        h_col = high_cols[0] if high_cols else result_cols[0]
        l_col = low_cols[0] if low_cols else (result_cols[1] if len(result_cols) > 1 else result_cols[0])
        if h_col == l_col and len(result_cols) > 1:
            # Final fallback to left/right split.
            h_col = result_cols[0]
            l_col = result_cols[1]
        return h_col, l_col

    hqc_result_col, lqc_result_col = detect_result_level_cols()

    records = []
    for row_idx in range(header_row_idx + 1, len(df)):
        row = df.iloc[row_idx]
        if pd.isna(row.iloc[hqc_result_col]) and pd.isna(row.iloc[lqc_result_col]):
            continue

        run_date = parse_date_value(row.iloc[date_col]) if date_col is not None else None
        if run_date is None:
            continue

        replicate = None
        if run_col is not None:
            try:
                replicate_val = row.iloc[run_col]
                if pd.notna(replicate_val):
                    replicate = int(float(replicate_val))
            except Exception:
                replicate = None

        for qc_level, col_idx in [("High", hqc_result_col), ("Low", lqc_result_col)]:
            raw_value = row.iloc[col_idx]
            if pd.isna(raw_value) or str(raw_value).strip() == "":
                continue
            try:
                concentration = float(raw_value)
            except Exception:
                continue

            records.append({
                "analyte": analyte,
                "qc_level": qc_level,
                "run_date": run_date,
                "concentration": concentration,
                "replicate": replicate if replicate is not None else 1,
            })

    return records


def get_qc_target(analyte_name, qc_level, as_of_date=None, db_path=None):
    db_path = db_path or DB_PATH
    if not Path(db_path).exists():
        return None
    analyte_name = normalize_analyte_name(analyte_name)

    # Normalize as_of_date to YYYY-MM-DD string
    if as_of_date is None:
        as_of_date = datetime.today().strftime("%Y-%m-%d")
    else:
        try:
            # Accept date/datetime objects or strings
            if not isinstance(as_of_date, str):
                as_of_date = pd.to_datetime(as_of_date, errors='coerce').strftime("%Y-%m-%d")
            else:
                parsed = pd.to_datetime(as_of_date, errors='coerce')
                if pd.isna(parsed):
                    as_of_date = datetime.today().strftime("%Y-%m-%d")
                else:
                    as_of_date = parsed.strftime("%Y-%m-%d")
        except Exception:
            as_of_date = datetime.today().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(db_path))
    # Primary lookup: effective range covering the as_of_date
    query = """
        SELECT qt.target_mean, qt.target_sd
        FROM qc_targets qt
        JOIN analytes a ON qt.analyte_id = a.analyte_id
        WHERE lower(a.name) = lower(?)
          AND qt.qc_level = ?
          AND qt.effective_from <= ?
          AND (qt.effective_to IS NULL OR qt.effective_to >= ?)
                ORDER BY qt.effective_from DESC, qt.target_id DESC
        LIMIT 1
    """
    row = conn.execute(query, (analyte_name, qc_level, as_of_date, as_of_date)).fetchone()
    if not row:
        # Fallback 1: most recent effective_from <= as_of_date (ignore effective_to)
        query2 = """
            SELECT qt.target_mean, qt.target_sd
            FROM qc_targets qt
            JOIN analytes a ON qt.analyte_id = a.analyte_id
            WHERE lower(a.name) = lower(?)
              AND qt.qc_level = ?
              AND qt.effective_from <= ?
                        ORDER BY qt.effective_from DESC, qt.target_id DESC
            LIMIT 1
        """
        row = conn.execute(query2, (analyte_name, qc_level, as_of_date)).fetchone()

    if not row:
        # Fallback 2: most recent target regardless of date
        query3 = """
            SELECT qt.target_mean, qt.target_sd
            FROM qc_targets qt
            JOIN analytes a ON qt.analyte_id = a.analyte_id
            WHERE lower(a.name) = lower(?)
              AND qt.qc_level = ?
                        ORDER BY qt.effective_from DESC, qt.target_id DESC
            LIMIT 1
        """
        row = conn.execute(query3, (analyte_name, qc_level)).fetchone()

    conn.close()
    if row:
        return {"target_mean": row[0], "target_sd": row[1]}
    return None


def get_per_date_targets(analyte_name, qc_level, run_dates, db_path=None):
    """Return a list of (mean, sd) tuples — one per run_date — using the target active on each date."""
    return [
        get_qc_target(analyte_name, qc_level, as_of_date=d, db_path=db_path)
        for d in run_dates
    ]


def get_all_qc_targets(db_path=None):
    """Return all rows from qc_targets joined with analyte names."""
    db_path = db_path or DB_PATH
    if not Path(db_path).exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(db_path))
    df = pd.read_sql_query("""
        SELECT qt.target_id, a.name AS analyte, qt.qc_level, qt.lot_number,
               qt.target_mean, qt.target_sd, qt.effective_from, qt.effective_to
        FROM qc_targets qt
        JOIN analytes a ON qt.analyte_id = a.analyte_id
        ORDER BY a.name, qt.qc_level, qt.effective_from
    """, conn)
    conn.close()
    return df


def insert_qc_target(analyte_name, qc_level, target_mean, target_sd,
                     effective_from, effective_to=None, lot_number=None, db_path=None):
    """Insert or replace a QC target row. Also closes the previous open row for that analyte/level."""
    db_path = db_path or DB_PATH
    analyte_name = normalize_analyte_name(analyte_name)
    ensure_db_initialized(db_path)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT analyte_id FROM analytes WHERE lower(name) = lower(?) ORDER BY analyte_id LIMIT 1",
        (analyte_name,)
    )
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO analytes (name, panel, display_order) VALUES (?, ?, ?)",
            (analyte_name, 1, None)
        )
        analyte_id = cursor.lastrowid
    else:
        analyte_id = row[0]

    # Close the previously open-ended row for this analyte+level (set effective_to = effective_from - 1 day)
    cursor.execute("""
        UPDATE qc_targets
        SET effective_to = date(?, '-1 day')
        WHERE analyte_id = ? AND qc_level = ? AND effective_to IS NULL
          AND effective_from < ?
    """, (effective_from, analyte_id, qc_level, effective_from))

    cursor.execute("""
        INSERT INTO qc_targets (analyte_id, qc_level, lot_number, target_mean, target_sd, effective_from, effective_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(analyte_id, qc_level, lot_number, effective_from) DO UPDATE SET
            target_mean=excluded.target_mean,
            target_sd=excluded.target_sd,
            effective_to=excluded.effective_to
    """, (analyte_id, qc_level, lot_number or "", target_mean, target_sd, effective_from, effective_to))

    conn.commit()
    conn.close()


def import_tecan_qc_file(file_bytes, filename, db_path=None):
    """Import Tecan format QC targets from Excel (multiple sheets with HQC/LQC structure)."""
    db_path = db_path or DB_PATH
    ensure_db_initialized(db_path)
    
    try:
        xls = pd.ExcelFile(BytesIO(file_bytes))
    except Exception as e:
        raise ValueError(f"Failed to read Excel file: {str(e)}")
    
    # Skip clearly non-analyte sheets (robust to spacing/case differences)
    analyte_sheets = [s for s in xls.sheet_names if not is_non_analyte_sheet(s)]
    
    if not analyte_sheets:
        return "No analyte sheets found in workbook."
    
    imported = 0
    imported_from_summary = 0
    skipped_inconsistent = 0
    skipped_no_summary = 0
    default_from = extract_date_from_filename(filename) or datetime.today().strftime("%Y-%m-%d")
    
    for sheet_name in analyte_sheets:
        # Read the sheet
        df = pd.read_excel(BytesIO(file_bytes), sheet_name=sheet_name, header=None)

        analyte_name = find_analyte_name_in_workbook(
            sheet_name,
            sample_row=df.iloc[0].tolist() if len(df) > 0 else None
        )
        if analyte_name is None:
            continue
        analyte_name = normalize_analyte_name(analyte_name)

        # Preferred path: use explicit target mean/SD from QC summary structure.
        summary_targets = parse_qc_targets_from_sheet(sheet_name, df, default_from)
        if summary_targets:
            for target in summary_targets:
                try:
                    insert_qc_target(
                        target["analyte"],
                        target["qc_level"],
                        float(target["target_mean"]),
                        float(target["target_sd"]),
                        target["effective_from"],
                        db_path=db_path,
                    )
                    imported += 1
                    imported_from_summary += 1
                except Exception:
                    pass
        else:
            skipped_no_summary += 1

    msg = f"Imported {imported} QC target(s) from Tecan format."
    if imported:
        msg += f" (summary-table based: {imported_from_summary})"
    if skipped_no_summary:
        msg += f" Skipped {skipped_no_summary} sheet(s) with no parseable HQC/LQC summary table."
    if skipped_inconsistent:
        msg += f" Skipped {skipped_inconsistent} target(s) due to large deviation from previous same-level target."
    return msg


def import_qc_targets_file(file_bytes, filename, db_path=None):
    """Import mean/SD targets from CSV/Excel file into qc_targets."""
    db_path = db_path or DB_PATH
    ensure_db_initialized(db_path)

    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(BytesIO(file_bytes))
    elif suffix in {".xls", ".xlsx"}:
        # Try to detect if it's Tecan format (has sheet names with analyte names)
        try:
            xls = pd.ExcelFile(BytesIO(file_bytes))
            analyte_like_sheets = [s for s in xls.sheet_names if not is_non_analyte_sheet(s)]
            if len(analyte_like_sheets) >= 2 or any("tecan" in s.lower() for s in xls.sheet_names):
                # Likely workbook-style QC chart file: try dedicated parser first.
                tecan_msg = import_tecan_qc_file(file_bytes, filename, db_path)
                if not tecan_msg.startswith("Imported 0"):
                    return tecan_msg
        except:
            pass
        
        df = pd.read_excel(BytesIO(file_bytes))
    else:
        raise ValueError("Unsupported target file type. Please upload CSV or Excel.")

    if df.empty:
        return "No target rows found in file."

    col_map = {str(c).strip().lower(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in col_map:
                return col_map[n]
        return None

    analyte_col = pick("analyte", "hormone", "compound", "name")
    level_col = pick("qc_level", "qc level", "level", "type")
    mean_col = pick("target_mean", "target mean", "qc mean", "qc_mean")
    sd_col = pick("target_sd", "target sd", "qc sd", "qc_sd", "sd")
    from_col = pick("effective_from", "effective from", "from", "date")
    to_col = pick("effective_to", "effective to", "to")
    lot_col = pick("lot_number", "lot number", "lot")

    # Build helpful error message if columns are missing
    missing = []
    if not analyte_col:
        missing.append("analyte (try: analyte, hormone, compound, name)")
    if not level_col:
        missing.append("qc_level (try: qc_level, qc level, level, type)")
    if not mean_col:
        missing.append("target_mean (try: target_mean, target mean, qc mean)")
    if not sd_col:
        missing.append("target_sd (try: target_sd, target sd, qc sd, sd)")
    
    if missing:
        available = ", ".join([f"'{c}'" for c in df.columns])
        raise ValueError(
            f"Targets file is missing: {' + '.join(missing)}.\n"
            f"Available columns: {available}"
        )

    default_from = extract_date_from_filename(filename) or datetime.today().strftime("%Y-%m-%d")
    imported = 0
    skipped = 0

    for _, row in df.iterrows():
        analyte_name = normalize_analyte_name(str(row[analyte_col]).strip()) if pd.notna(row[analyte_col]) else ""
        if not analyte_name:
            skipped += 1
            continue

        qc_level = normalize_qc_level(row[level_col])
        if qc_level is None:
            skipped += 1
            continue

        try:
            target_mean = float(row[mean_col])
            target_sd = float(row[sd_col])
        except Exception:
            skipped += 1
            continue

        effective_from = parse_date_value(row[from_col]) if from_col is not None else None
        effective_from = effective_from or default_from
        effective_to = parse_date_value(row[to_col]) if to_col is not None else None
        lot_number = str(row[lot_col]).strip() if lot_col is not None and pd.notna(row[lot_col]) else None

        insert_qc_target(
            analyte_name=analyte_name,
            qc_level=qc_level,
            target_mean=target_mean,
            target_sd=target_sd,
            effective_from=effective_from,
            effective_to=effective_to,
            lot_number=lot_number,
            db_path=db_path,
        )
        imported += 1

    return f"Imported/updated {imported} target rows" + (f" (skipped {skipped})" if skipped else "")


def import_excel_qc_file(file_bytes, filename, db_path=None, uploaded_by=None):
    """Import QC measurement or mean-value Excel data into the QC database."""
    ensure_db_initialized(db_path)
    conn = get_connection(db_path)
    cursor = conn.cursor()

    uploaded_by = str(uploaded_by).strip().upper() if uploaded_by else None

    source_filename = Path(filename).name
    cursor.execute("SELECT run_id FROM runs WHERE source_filename = ?", (source_filename,))
    if cursor.fetchone():
        conn.close()
        return f"Already imported: {source_filename}"

    excel_file = pd.ExcelFile(BytesIO(file_bytes))
    file_date = extract_date_from_filename(source_filename)

    analyte_sheets = []
    for sheet in excel_file.sheet_names:
        analyte = find_analyte_name_in_workbook(sheet)
        if analyte is not None:
            analyte_sheets.append((sheet, analyte))

    records = []
    qc_targets = []
    if analyte_sheets:
        sheet_date = file_date
        for sheet_name, analyte in analyte_sheets:
            df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
            sheet_records = parse_qc_run_rows_from_sheet(sheet_name, df_sheet)
            records.extend(sheet_records)
            if sheet_date is None and sheet_records:
                found_dates = [r["run_date"] for r in sheet_records if r.get("run_date")]
                if found_dates:
                    sheet_date = min(found_dates)
        for sheet_name, analyte in analyte_sheets:
            df_sheet = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)
            qc_targets.extend(parse_qc_targets_from_sheet(sheet_name, df_sheet, sheet_date or datetime.today().strftime("%Y-%m-%d")))
    else:
        df_raw = pd.read_excel(BytesIO(file_bytes))
        if df_raw.empty:
            conn.close()
            return "The Excel file contains no data."

        df = df_raw.copy()
        analyte_col = find_column(df, ["analyte", "compound", "name", "assay"])
        date_col = find_column(df, ["date", "run date", "run_date", "measurement date", "sample date"])
        qc_level_col = find_column(df, ["qc level", "level", "type", "sample type"])
        value_col = find_column(df, ["concentration", "mean", "value", "result", "measurement"])
        replicate_col = find_column(df, ["replicate", "rep", "replicate number"])

        hqc_value_columns = [col for col in df.columns if any(token in str(col).strip().lower() for token in ["hqc", "high"]) and any(token in str(col).strip().lower() for token in ["mean", "conc"])]
        lqc_value_columns = [col for col in df.columns if any(token in str(col).strip().lower() for token in ["lqc", "low"]) and any(token in str(col).strip().lower() for token in ["mean", "conc"])]

        if analyte_col is None:
            conn.close()
            raise ValueError("Excel file must include an analyte column (for example, Analyte, Compound, or Name).")

        if hqc_value_columns or lqc_value_columns:
            for _, row in df.iterrows():
                analyte = normalize_analyte_name(str(row[analyte_col]).strip()) if pd.notna(row[analyte_col]) else None
                if not analyte:
                    continue

                run_date = parse_date_value(row[date_col]) if date_col is not None else None
                run_date = run_date or file_date
                if run_date is None:
                    raise ValueError("Excel file must include a date column or the filename must contain a date.")

                for col in hqc_value_columns:
                    concentration = row[col]
                    if pd.isna(concentration) or str(concentration).strip() == "":
                        continue
                    records.append({
                        "analyte": analyte,
                        "qc_level": "High",
                        "run_date": run_date,
                        "concentration": float(concentration),
                        "replicate": int(row[replicate_col]) if replicate_col and pd.notna(row[replicate_col]) else 1,
                    })

                for col in lqc_value_columns:
                    concentration = row[col]
                    if pd.isna(concentration) or str(concentration).strip() == "":
                        continue
                    records.append({
                        "analyte": analyte,
                        "qc_level": "Low",
                        "run_date": run_date,
                        "concentration": float(concentration),
                        "replicate": int(row[replicate_col]) if replicate_col and pd.notna(row[replicate_col]) else 1,
                    })
        elif qc_level_col is not None and value_col is not None:
            for _, row in df.iterrows():
                analyte = normalize_analyte_name(str(row[analyte_col]).strip()) if pd.notna(row[analyte_col]) else None
                if not analyte:
                    continue

                qc_level = normalize_qc_level(row[qc_level_col])
                if qc_level is None:
                    continue

                run_date = parse_date_value(row[date_col]) if date_col is not None else None
                run_date = run_date or file_date
                if run_date is None:
                    raise ValueError("Excel file must include a date column or the filename must contain a date.")

                concentration = row[value_col]
                if pd.isna(concentration) or str(concentration).strip() == "":
                    continue

                records.append({
                    "analyte": analyte,
                    "qc_level": qc_level,
                    "run_date": run_date,
                    "concentration": float(concentration),
                    "replicate": int(row[replicate_col]) if replicate_col and pd.notna(row[replicate_col]) else 1,
                })
        else:
            conn.close()
            raise ValueError(
                "Excel import requires either HQC/LQC value columns (e.g. HQC Mean, LQC Mean) or a QC Level column plus a concentration/mean column."
            )

    if not records and not qc_targets:
        conn.close()
        return "No QC records were found in the Excel file."

    run_date = records[0]["run_date"] if records else file_date or datetime.today().strftime("%Y-%m-%d")
    cursor.execute(
        "INSERT INTO runs (run_date, panel, source_filename, method_name, data_path, uploaded_by) VALUES (?, ?, ?, ?, ?, ?)",
        (run_date, 1, source_filename, None, None, uploaded_by)
    )
    run_id = cursor.lastrowid

    if qc_targets:
        for target in qc_targets:
            try:
                insert_qc_target(
                    analyte_name=target["analyte"],
                    qc_level=target["qc_level"],
                    target_mean=float(target["target_mean"]),
                    target_sd=float(target["target_sd"]),
                    effective_from=target["effective_from"],
                    db_path=db_path,
                )
            except Exception:
                # Keep QC result import resilient even if one target row fails.
                pass

    analyte_id_map = {}
    for record in records:
        analyte = record["analyte"]
        if analyte not in analyte_id_map:
            conn.execute(
                "INSERT OR IGNORE INTO analytes (name, panel, display_order) VALUES (?, ?, ?)",
                (analyte, 1, None)
            )
            cursor.execute(
                "SELECT analyte_id FROM analytes WHERE lower(name) = lower(?) ORDER BY analyte_id LIMIT 1",
                (analyte,)
            )
            row = cursor.fetchone()
            if row:
                analyte_id_map[analyte] = row[0]

    cursor.execute("SELECT type_code, type_id FROM sample_types")
    type_map = dict(cursor.fetchall())

    imported_count = 0
    for index, record in enumerate(records, start=1):
        analyte = record["analyte"]
        if analyte not in analyte_id_map:
            continue

        qc_level = record["qc_level"]
        concentration = record["concentration"]
        run_date = record["run_date"]
        replicate = record["replicate"]

        data_filename = f"QC_{qc_level}_{analyte}_{run_date}_{replicate}"
        data_filename = re.sub(r"[^A-Za-z0-9_.-]", "_", data_filename)

        sample_type_id = type_map["qc"]
        cursor.execute(
            "INSERT INTO samples (run_id, data_filename, sample_name, sample_type_id, collection_date, qc_level, qc_replicate) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, data_filename, analyte, sample_type_id, run_date, qc_level, replicate)
        )
        sample_id = cursor.lastrowid

        cursor.execute(
            "INSERT OR REPLACE INTO results (sample_id, analyte_id, concentration) VALUES (?, ?, ?)",
            (sample_id, analyte_id_map[analyte], concentration)
        )
        imported_count += 1

    conn.commit()
    conn.close()
    return f"Imported {imported_count} QC records from {source_filename}."


# ==============================================================================
# QC DATA QUERIES
# ==============================================================================

def get_qc_data(db_path=None):
    """Pull all QC results from the database."""
    db_path = db_path or DB_PATH
    if not db_path.exists():
        return pd.DataFrame()

    conn = get_connection(db_path)
    query = """
        SELECT
            r.run_date,
            a.name as analyte,
            s.qc_level,
            r.uploaded_by,
            AVG(res.concentration) as concentration
        FROM results res
        JOIN samples s ON res.sample_id = s.sample_id
        JOIN runs r ON s.run_id = r.run_id
        JOIN analytes a ON res.analyte_id = a.analyte_id
        JOIN sample_types st ON s.sample_type_id = st.type_id
        WHERE st.type_code = 'qc'
          AND res.concentration IS NOT NULL
        GROUP BY r.run_date, a.name, s.qc_level, r.uploaded_by
        ORDER BY a.name, r.run_date
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def get_qc_chart_data(db_path=None):
    """Pull raw QC points (no date averaging) for dashboard charting."""
    db_path = db_path or DB_PATH
    if not db_path.exists():
        return pd.DataFrame()

    conn = get_connection(db_path)
    query = """
        SELECT
            COALESCE(s.collection_date, substr(s.acquisition_datetime, 1, 10), r.run_date) AS run_date,
            a.name as analyte,
            s.qc_level,
            r.uploaded_by,
            s.sample_id,
            res.concentration
        FROM results res
        JOIN samples s ON res.sample_id = s.sample_id
        JOIN runs r ON s.run_id = r.run_id
        JOIN analytes a ON res.analyte_id = a.analyte_id
        JOIN sample_types st ON s.sample_type_id = st.type_id
        WHERE st.type_code = 'qc'
          AND res.concentration IS NOT NULL
        ORDER BY a.name, s.qc_level, COALESCE(s.collection_date, substr(s.acquisition_datetime, 1, 10), r.run_date), s.sample_id
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


def query_run_summary(db_path=None):
    """Get summary of all runs."""
    db_path = db_path or DB_PATH
    if not db_path.exists():
        return pd.DataFrame()

    conn = get_connection(db_path)
    query = """
        SELECT
            r.run_id        AS "ID",
            r.run_date      AS "Run Date",
            r.panel         AS "Panel",
            r.method_name   AS "Method",
            r.source_filename AS "File Name",
            r.uploaded_by   AS "Uploaded By",
            r.imported_at   AS "Imported At",
            COUNT(DISTINCT s.sample_id) AS "Samples"
        FROM runs r
        LEFT JOIN samples s ON r.run_id = s.run_id
        GROUP BY r.run_id ORDER BY r.run_date DESC
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    return df


# ==============================================================================
# QC EXPORT FUNCTIONS
# ==============================================================================

def format_date(date_str):
    """Convert YYYY-MM-DD to DD/MM/YYYY."""
    parts = date_str.split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}"


def export_analyte_csv(analyte_name, hqc_data, lqc_data):
    """Create CSV data for one analyte with HQC and LQC side by side."""
    hqc_mean = hqc_data["concentration"].mean() if not hqc_data.empty else np.nan
    hqc_sd = hqc_data["concentration"].std() if len(hqc_data) > 1 else np.nan

    lqc_mean = lqc_data["concentration"].mean() if not lqc_data.empty else np.nan
    lqc_sd = lqc_data["concentration"].std() if len(lqc_data) > 1 else np.nan

    all_dates = sorted(set(hqc_data["run_date"].tolist() + lqc_data["run_date"].tolist()))

    hqc_by_date = dict(zip(hqc_data["run_date"], hqc_data["concentration"]))
    lqc_by_date = dict(zip(lqc_data["run_date"], lqc_data["concentration"]))

    rows = []
    uploader_by_date = {}
    for row_info in pd.concat([hqc_data, lqc_data]).to_dict("records"):
        run_date = row_info.get("run_date")
        if run_date:
            uploader_by_date[run_date] = row_info.get("uploaded_by", "")

    for date in all_dates:
        row = {"Date": format_date(date), "User_Initials": uploader_by_date.get(date, "")}

        hqc_conc = hqc_by_date.get(date)
        row["HQC_Conc"] = round(hqc_conc, 4) if hqc_conc is not None else ""
        row["HQC_Mean"] = round(hqc_mean, 4) if not np.isnan(hqc_mean) else ""
        row["HQC_+2SD"] = round(hqc_mean + 2 * hqc_sd, 4) if not np.isnan(hqc_sd) else ""
        row["HQC_-2SD"] = round(hqc_mean - 2 * hqc_sd, 4) if not np.isnan(hqc_sd) else ""
        row["HQC_+3SD"] = round(hqc_mean + 3 * hqc_sd, 4) if not np.isnan(hqc_sd) else ""
        row["HQC_-3SD"] = round(hqc_mean - 3 * hqc_sd, 4) if not np.isnan(hqc_sd) else ""

        lqc_conc = lqc_by_date.get(date)
        row["LQC_Conc"] = round(lqc_conc, 4) if lqc_conc is not None else ""
        row["LQC_Mean"] = round(lqc_mean, 4) if not np.isnan(lqc_mean) else ""
        row["LQC_+2SD"] = round(lqc_mean + 2 * lqc_sd, 4) if not np.isnan(lqc_sd) else ""
        row["LQC_-2SD"] = round(lqc_mean - 2 * lqc_sd, 4) if not np.isnan(lqc_sd) else ""
        row["LQC_+3SD"] = round(lqc_mean + 3 * lqc_sd, 4) if not np.isnan(lqc_sd) else ""
        row["LQC_-3SD"] = round(lqc_mean - 3 * lqc_sd, 4) if not np.isnan(lqc_sd) else ""

        rows.append(row)

    return pd.DataFrame(rows)


# ==============================================================================
# QC CHART FUNCTIONS
# ==============================================================================

def flag_outliers(concentrations, sd2_upper, sd2_lower, sd3_upper, sd3_lower):
    """Flag values exceeding 2SD or 3SD bands."""
    flags = [False] * len(concentrations)

    for i, conc in enumerate(concentrations):
        if conc > sd3_upper or conc < sd3_lower:
            flags[i] = True

        if conc > sd2_upper or conc < sd2_lower:
            if i > 0 and (concentrations[i - 1] > sd2_upper or concentrations[i - 1] < sd2_lower):
                flags[i] = True
                flags[i - 1] = True

    return flags


def make_qc_chart(dates, concentrations, mean_val, sd2_upper, sd2_lower, sd3_upper, sd3_lower, title, uploader_initials=None, means_per_point=None, sds_per_point=None):
    """Create Levey-Jennings chart with 2SD/3SD bands.
    If means_per_point/sds_per_point are supplied they override the flat mean_val/sdX_upper/lower
    and the reference lines are drawn as step functions."""
    # Build per-point SD band values
    if means_per_point and sds_per_point and len(means_per_point) == len(dates):
        pp_mean    = [float(m) for m in means_per_point]
        pp_sd2_u   = [float(m) + 2 * float(s) for m, s in zip(means_per_point, sds_per_point)]
        pp_sd2_l   = [float(m) - 2 * float(s) for m, s in zip(means_per_point, sds_per_point)]
        pp_sd3_u   = [float(m) + 3 * float(s) for m, s in zip(means_per_point, sds_per_point)]
        pp_sd3_l   = [float(m) - 3 * float(s) for m, s in zip(means_per_point, sds_per_point)]
        # also recalculate the flat values used for flagging as the last active target
        mean_val   = pp_mean[-1]
        sd2_upper  = pp_sd2_u[-1]
        sd2_lower  = pp_sd2_l[-1]
        sd3_upper  = pp_sd3_u[-1]
        sd3_lower  = pp_sd3_l[-1]
    else:
        pp_mean  = [mean_val]  * len(dates)
        pp_sd2_u = [sd2_upper] * len(dates)
        pp_sd2_l = [sd2_lower] * len(dates)
        pp_sd3_u = [sd3_upper] * len(dates)
        pp_sd3_l = [sd3_lower] * len(dates)

    flags = flag_outliers(concentrations, sd2_upper, sd2_lower, sd3_upper, sd3_lower)
    initials_list = [(str(x).strip().upper() if pd.notna(x) and str(x).strip() else "NA") for x in (uploader_initials if uploader_initials is not None else [None] * len(dates))]
    if len(initials_list) != len(dates):
        initials_list = [initials_list[0] if initials_list else "NA"] * len(dates)

    fig = go.Figure()

    # Keep each point in table order, even when dates repeat.
    x_pos = list(range(len(dates)))

    # Step reference lines (change at each effective date)
    fig.add_trace(go.Scatter(
        x=x_pos, y=pp_mean,
        mode="lines",
        line=dict(color="#008000", dash="dash", width=2, shape="hv"),
        name="Mean",
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_pos, y=pp_sd2_u,
        mode="lines",
        line=dict(color="#ff9800", dash="dot", width=1, shape="hv"),
        name="+2SD",
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_pos, y=pp_sd2_l,
        mode="lines",
        line=dict(color="#ff9800", dash="dot", width=1, shape="hv"),
        name="-2SD",
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_pos, y=pp_sd3_u,
        mode="lines",
        line=dict(color="#ff3d00", dash="dash", width=1, shape="hv"),
        name="+3SD",
        hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=x_pos, y=pp_sd3_l,
        mode="lines",
        line=dict(color="#ff3d00", dash="dash", width=1, shape="hv"),
        name="-3SD",
        hoverinfo="skip",
    ))

    # Per-point hover data includes that point's active mean/SD
    customdata_main = [
        [ini, pm, psu, psl, pu3, pl3, d]
        for ini, pm, psu, psl, pu3, pl3, d
        in zip(initials_list, pp_mean, pp_sd2_u, pp_sd2_l, pp_sd3_u, pp_sd3_l, dates)
    ]
    fig.add_trace(go.Scatter(
        x=x_pos,
        y=concentrations,
        mode="lines+markers",
        marker=dict(size=9, color="#1976d2"),
        line=dict(color="#1976d2", width=2),
        name="Concentration",
        customdata=customdata_main,
        hovertemplate=(
            "Date: %{customdata[6]}<br>"
            "Concentration: %{y:.3f}<br>"
            "Initials: %{customdata[0]}<br>"
            "Mean: %{customdata[1]:.3f}<br>"
            "+2SD: %{customdata[2]:.3f}<br>"
            "-2SD: %{customdata[3]:.3f}<br>"
            "+3SD: %{customdata[4]:.3f}<br>"
            "-3SD: %{customdata[5]:.3f}<extra></extra>"
        ),
    ))

    flagged_dates = [x for x, f in zip(x_pos, flags) if f]
    flagged_concs = [c for c, f in zip(concentrations, flags) if f]
    flagged_initials = [ini for ini, f in zip(initials_list, flags) if f]
    flagged_custom = [cd for cd, f in zip(customdata_main, flags) if f]

    if flagged_dates:
        fig.add_trace(go.Scatter(
            x=flagged_dates, y=flagged_concs,
            mode="markers",
            marker=dict(size=12, color="#d32f2f", symbol="triangle-up", line=dict(width=1, color="#b71c1c")),
            name="Flagged",
            customdata=flagged_custom,
            hovertemplate=(
                "Date: %{customdata[6]}<br>"
                "Concentration: %{y:.3f}<br>"
                "Initials: %{customdata[0]}<br>"
                "Mean: %{customdata[1]:.3f}<br>"
                "+2SD: %{customdata[2]:.3f}<br>"
                "-2SD: %{customdata[3]:.3f}<br>"
                "+3SD: %{customdata[4]:.3f}<br>"
                "-3SD: %{customdata[5]:.3f}<extra></extra>"
            ),
        ))

    fig.update_layout(
        title=dict(text=title, y=0.97, yanchor="top"),
        xaxis_title="Date",
        yaxis_title="Concentration",
        height=420,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#666666"),
        legend=dict(orientation="h", yanchor="bottom", y=1.08, xanchor="right", x=1),
        margin=dict(t=115, b=40, l=60, r=20),
    )

    fig.update_xaxes(
        showgrid=True,
        gridcolor="rgba(200,200,200,0.3)",
        zeroline=False,
        tickmode="array",
        tickvals=x_pos,
        ticktext=dates,
        tickangle=-45,
        title_standoff=10,
    )
    fig.update_yaxes(
        showgrid=True,
        gridcolor="rgba(200,200,200,0.3)",
        zeroline=False,
        tickformat=".3f",
        title_standoff=10,
    )

    return fig


def create_value_pictogram(concentrations, mean_val, sd_val):
    """Create an inline SVG pictogram (SD lines, history dots, recent circle)."""
    if not concentrations:
        svg = (
            "<svg xmlns='http://www.w3.org/2000/svg' width='220' height='56'>"
            "<rect x='1' y='1' width='218' height='54' rx='5' fill='none' stroke='#bdbdbd'/>"
            "<text x='110' y='34' text-anchor='middle' font-size='12' fill='#9e9e9e'>N/A</text>"
            "</svg>"
        )
        return f"data:image/svg+xml;utf8,{quote(svg)}"

    w = 220
    h = 56
    left = 10
    right = w - 10
    y_mid = 28
    y_hist = 40

    if sd_val and sd_val > 0 and not pd.isna(sd_val):
        effective_sd = float(sd_val)
    else:
        data_min = min(concentrations)
        data_max = max(concentrations)
        span = max(data_max - data_min, 1e-6)
        effective_sd = max(span / 6.0, abs(float(mean_val)) * 0.05, 1e-6)

    lo = float(mean_val) - 3 * effective_sd
    hi = float(mean_val) + 3 * effective_sd

    def x_of(value):
        ratio = (float(value) - lo) / max(hi - lo, 1e-9)
        ratio = min(max(ratio, 0.0), 1.0)
        return left + ratio * (right - left)

    x_mean = x_of(mean_val)
    x_recent = x_of(concentrations[-1])
    x_2sd_l = x_of(mean_val - 2 * effective_sd)
    x_2sd_r = x_of(mean_val + 2 * effective_sd)
    x_3sd_l = x_of(mean_val - 3 * effective_sd)
    x_3sd_r = x_of(mean_val + 3 * effective_sd)

    hist_vals = concentrations[:-1][-8:]
    hist_circles = "".join(
        f"<circle cx='{x_of(v):.1f}' cy='{y_hist}' r='3.8' fill='#455a64' opacity='0.9'/>"
        for v in hist_vals
    )

    svg = f"""
<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}'>
  <rect x='1' y='1' width='{w - 2}' height='{h - 2}' rx='6' fill='none' stroke='#90a4ae' stroke-width='1'/>
  <line x1='{left}' y1='{y_mid}' x2='{right}' y2='{y_mid}' stroke='#90a4ae' stroke-width='1.2'/>
  <line x1='{x_3sd_l:.1f}' y1='14' x2='{x_3sd_l:.1f}' y2='42' stroke='#ef5350' stroke-width='2'/>
  <line x1='{x_3sd_r:.1f}' y1='14' x2='{x_3sd_r:.1f}' y2='42' stroke='#ef5350' stroke-width='2'/>
  <line x1='{x_2sd_l:.1f}' y1='17' x2='{x_2sd_l:.1f}' y2='39' stroke='#ff9800' stroke-width='1.8'/>
  <line x1='{x_2sd_r:.1f}' y1='17' x2='{x_2sd_r:.1f}' y2='39' stroke='#ff9800' stroke-width='1.8'/>
  <line x1='{x_mean:.1f}' y1='12' x2='{x_mean:.1f}' y2='44' stroke='#111111' stroke-width='2.2'/>
  {hist_circles}
  <circle cx='{x_recent:.1f}' cy='{y_mid}' r='8.5' fill='#fff200' stroke='#111111' stroke-width='1.6'/>
  <circle cx='{x_mean:.1f}' cy='{y_mid}' r='3.3' fill='#111111'/>
</svg>
""".strip()

    return f"data:image/svg+xml;utf8,{quote(svg)}"


def generate_final_report(db_path=None):
    """Generate comprehensive QC report for all analytes."""
    db_path = db_path or DB_PATH
    if not db_path.exists():
        return None
    
    df_qc = get_qc_data(db_path)
    if df_qc.empty:
        return None
    
    analytes = sorted(df_qc["analyte"].unique())
    report_data = []
    
    for analyte in analytes:
        analyte_data = df_qc[df_qc["analyte"] == analyte]
        dashboard_url = f"?mode=Dashboard&analyte={quote(str(analyte))}"
        
        for qc_level in ["High", "Low"]:
            level_data = analyte_data[analyte_data["qc_level"] == qc_level].reset_index(drop=True)
            
            if level_data.empty:
                report_data.append({
                    "Analyte": analyte,
                    "Go to Dashboard": dashboard_url,
                    "QC Level": "HQC" if qc_level == "High" else "LQC",
                    "Pictogram": create_value_pictogram([], 0.0, 0.0),
                    "Recent": "—",
                    "Mean": "—",
                    "Min": "—",
                    "Max": "—",
                    "N": 0,
                    "Status": "N/A"
                })
                continue
            
            concentrations = level_data["concentration"].tolist()
            recent = concentrations[-1]
            mean_val = level_data["concentration"].mean()
            sd = level_data["concentration"].std() if len(level_data) > 1 else 0
            
            target = get_qc_target(analyte, qc_level, as_of_date=level_data["run_date"].max())
            if target:
                mean_val = float(target["target_mean"])
                sd = float(target["target_sd"])
            
            sd2_upper = mean_val + 2 * sd
            sd2_lower = mean_val - 2 * sd

            if pd.isna(sd) or float(sd) <= 0:
                status = "N/A (SD unavailable)"
            else:
                z_abs = abs((recent - mean_val) / sd)
                if z_abs > 3:
                    status = "⚠ Out of Range (>3 SD)"
                elif z_abs > 2:
                    status = "⚠ Out of Range (>2 SD)"
                else:
                    status = "✓ OK (within 2 SD)"
            
            report_data.append({
                "Analyte": analyte,
                "Go to Dashboard": dashboard_url,
                "QC Level": "HQC" if qc_level == "High" else "LQC",
                "Pictogram": create_value_pictogram(concentrations, mean_val, sd),
                "Recent": f"{recent:.3f}",
                "Mean": f"{mean_val:.3f}",
                "Min": f"{min(concentrations):.3f}",
                "Max": f"{max(concentrations):.3f}",
                "N": len(concentrations),
                "Status": status
            })
    
    return pd.DataFrame(report_data)


# ==============================================================================
# STREAMLIT APP
# ==============================================================================
def render_database_file_management():
    """Render database file download/delete controls (Database tab only)."""
    st.markdown("---")
    st.subheader("🗄️ Database File Management")
    st.code(f"{DB_PATH}", language="text")
    st.caption("Default DB path is repo-local unless QC_STUDIO_DB_PATH is set.")

    db_bytes = get_db_download_bytes()
    if db_bytes is None:
        st.info("Database file does not exist yet. Import a file first.")
    else:
        st.download_button(
            label="📥 Download current database (.db)",
            data=db_bytes,
            file_name=DB_PATH.name,
            mime="application/octet-stream",
            use_container_width=True,
        )

    with st.expander("⚠️ Danger Zone"):
        st.warning("This permanently deletes the database file and all imported data.")
        confirm_delete = st.checkbox("I understand this cannot be undone.", key="confirm_delete_db")
        if st.button("🗑️ Delete database file and reset", use_container_width=True, disabled=not confirm_delete):
            try:
                deleted = delete_database_file()
                if deleted:
                    st.success(f"Deleted database: {DB_PATH}")
                    st.rerun()
                else:
                    st.info("No database file found to delete.")
            except Exception as e:
                st.error(f"Failed to delete database: {e}")
                
def main():
    st.set_page_config(page_title="QC Studio", layout="wide")
    st.title("🧪 QC Studio")
    st.markdown("Integrated QC panel database, QC export, and dashboard platform")
    st.sidebar.caption(f"DB: {DB_PATH}")

    # Sidebar navigation
    module_options = ["Dashboard", "Database", "Export", "Report"]
    query_mode = str(st.query_params.get("mode", "")).strip()
    default_mode_idx = module_options.index(query_mode) if query_mode in module_options else 0
    mode = st.sidebar.radio(
        "Select Module",
        module_options,
        index=default_mode_idx,
        help="Choose between viewing QC charts, managing the database, exporting data, or generating a final report"
    )
    st.query_params["mode"] = mode

    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Database:** `{DB_PATH.name}`")

    if mode == "Database":
        st.header("📊 QC Panel Database")

        st.info("💡 **How it works:** Upload CSV or Excel files to automatically create and populate the database. The schema and reference data are generated on-demand from your first file upload.")

        col1, col2 = st.columns(2)

        with col1:
            initials = st.text_input(
                "Enter your initials",
                max_chars=6,
                key="qc_uploader_initials",
                help="Enter the initials of the user uploading this QC file. Leave blank if not available."
            )
            uploaded_file = st.file_uploader(
                "📁 Import QC Data (CSV or Excel)",
                type=["csv", "xls", "xlsx"],
                key="qc_data_uploader"
            )
            if uploaded_file:
                tmp_path = None
                try:
                    initials = str(initials).strip().upper() if initials else None
                    with st.spinner("Importing QC data..."):
                        suffix = Path(uploaded_file.name).suffix.lower()
                        if suffix == ".csv":
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                                tmp.write(uploaded_file.getbuffer())
                                tmp_path = tmp.name
                            result = import_csv(tmp_path, uploaded_by=initials, original_filename=uploaded_file.name)
                        elif suffix in {".xls", ".xlsx"}:
                            result = import_excel_qc_file(uploaded_file.read(), uploaded_file.name, uploaded_by=initials)
                        else:
                            raise ValueError("Unsupported file type. Upload a CSV or Excel file.")
                    st.success(result)
                    get_qc_data.clear() if hasattr(get_qc_data, 'clear') else None
                except Exception as e:
                    st.error(f"Import failed: {e}")
                finally:
                    if tmp_path and Path(tmp_path).exists():
                        os.unlink(tmp_path)

        st.markdown("---")
        render_database_file_management()
        
        st.subheader("📋 Run Summary")
        df_runs = query_run_summary()
        if df_runs.empty:
            st.info("No data imported yet. Upload a CSV file to get started.")
        else:
            st.dataframe(df_runs, use_container_width=True)

        st.markdown("---")
        st.subheader("🎯 QC Targets Manager")
        st.markdown("View existing mean/SD targets per analyte and add new ones when lot changes.")

        df_targets = get_all_qc_targets()
        if df_targets.empty:
            st.info("No QC targets stored yet. Import an Excel workbook or add one manually below.")
        else:
            st.dataframe(df_targets, use_container_width=True, hide_index=True)

        with st.expander("📤 Upload Mean/SD Targets File"):
            st.caption(
                "Upload CSV/Excel with columns: analyte, qc_level, target_mean, target_sd, "
                "and optional effective_from, effective_to, lot_number."
            )
            targets_file = st.file_uploader(
                "Upload targets file",
                type=["csv", "xls", "xlsx"],
                key="qc_targets_file_uploader",
            )
            if targets_file and st.button("⬆️ Import Targets File", use_container_width=True, key="import_targets_file_btn"):
                try:
                    msg = import_qc_targets_file(targets_file.read(), targets_file.name)
                    st.success(msg)
                    st.rerun()
                except Exception as e:
                    st.error(f"Targets import failed: {e}")

        with st.expander("➕ Add / Update QC Target"):
            if not DB_PATH.exists():
                st.warning("Import data first to initialise the database.")
            else:
                conn_tmp = get_connection()
                all_analyte_names = [
                    r[0]
                    for r in conn_tmp.execute(
                        """
                        SELECT MIN(name) AS name
                        FROM analytes
                        GROUP BY lower(name)
                        ORDER BY lower(name)
                        """
                    ).fetchall()
                ]
                conn_tmp.close()
                if not all_analyte_names:
                    st.info("No analytes in database yet. Import a data file first to populate analyte names.")
                    all_analyte_names = ["— no analytes —"]
                t_col1, t_col2 = st.columns(2)
                with t_col1:
                    t_analyte = st.selectbox("Analyte", all_analyte_names, key="t_analyte")
                    t_level   = st.selectbox("QC Level", ["High (HQC)", "Low (LQC)"], key="t_level")
                    t_lot     = st.text_input("Lot Number (optional)", key="t_lot")
                with t_col2:
                    t_mean    = st.number_input("Target Mean", min_value=0.0, format="%.4f", key="t_mean")
                    t_sd      = st.number_input("Target SD",   min_value=0.0, format="%.4f", key="t_sd")
                    t_from    = st.date_input("Effective From", key="t_from")
                    t_to      = st.date_input("Effective To (leave blank = open-ended)", value=None, key="t_to")

                if st.button("💾 Save QC Target", use_container_width=True):
                    try:
                        level_code = "High" if "High" in t_level else "Low"
                        insert_qc_target(
                            analyte_name  = t_analyte,
                            qc_level      = level_code,
                            target_mean   = t_mean,
                            target_sd     = t_sd,
                            effective_from= str(t_from),
                            effective_to  = str(t_to) if t_to else None,
                            lot_number    = t_lot or None,
                        )
                        st.success(f"Saved target for {t_analyte} {level_code} effective {t_from}.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to save: {e}")

    elif mode == "Export":
        st.header("📤 QC Export")

        if not DB_PATH.exists():
            st.error("Database not found. Import data first in the Database tab.")
            return

        df_qc = get_qc_data()
        if df_qc.empty:
            st.info("No QC data found in the database.")
            return

        analytes = sorted(df_qc["analyte"].unique())

        st.subheader("Export Analyte CSVs")
        st.markdown("Generate CSV files with HQC and LQC values for all analytes.")

        if st.button("📥 Generate All CSV Files", use_container_width=True):
            exported = []
            temp_dir = tempfile.mkdtemp()

            with st.spinner("Generating export files..."):
                for analyte in analytes:
                    analyte_data = df_qc[df_qc["analyte"] == analyte]
                    hqc_data = analyte_data[analyte_data["qc_level"] == "High"].reset_index(drop=True)
                    lqc_data = analyte_data[analyte_data["qc_level"] == "Low"].reset_index(drop=True)

                    if hqc_data.empty and lqc_data.empty:
                        continue

                    df_export = export_analyte_csv(analyte, hqc_data, lqc_data)
                    export_path = Path(temp_dir) / f"{analyte}_QC.csv"
                    df_export.to_csv(export_path, index=False)
                    exported.append((analyte, export_path))

            st.success(f"Generated {len(exported)} CSV files!")

            for analyte, export_path in exported:
                with open(export_path, "rb") as file:
                    st.download_button(
                        label=f"📥 Download {analyte}_QC.csv",
                        data=file.read(),
                        file_name=f"{analyte}_QC.csv",
                        mime="text/csv",
                        use_container_width=True
                    )

    elif mode == "Dashboard":
        st.header("📈 QC Dashboard")

        if not DB_PATH.exists():
            st.error("Database not found. Import data first in the Database tab.")
            return

        df = get_qc_chart_data()
        if df.empty:
            st.warning("No QC data found in the database. Import data to view charts.")
            return

        analytes = sorted(df["analyte"].unique())
        query_analyte = str(st.query_params.get("analyte", "")).strip()
        if not query_analyte:
            # Backward compatibility for existing shared links.
            query_analyte = str(st.query_params.get("hormone", "")).strip()
        default_analyte_idx = analytes.index(query_analyte) if query_analyte in analytes else 0
        selected = st.sidebar.radio("Select Analyte", analytes, index=default_analyte_idx)
        st.query_params["analyte"] = selected

        analyte_data = df[df["analyte"] == selected]
        hqc_data = analyte_data[analyte_data["qc_level"] == "High"].reset_index(drop=True)
        lqc_data = analyte_data[analyte_data["qc_level"] == "Low"].reset_index(drop=True)
        hqc_target_mean = None
        hqc_target_sd = None
        lqc_target_mean = None
        lqc_target_sd = None

        chart_cols = st.columns(2)

        if hqc_data.empty:
            chart_cols[0].info(f"No HQC data for {selected}.")
        else:
            hqc_concentrations = hqc_data["concentration"].tolist()
            hqc_raw_dates = hqc_data["run_date"].tolist()
            hqc_dates = [d.replace("-", "/") for d in hqc_raw_dates]
            if len(set(hqc_raw_dates)) <= 1:
                chart_cols[0].warning(
                    f"HQC points for {selected} are from a single run date, so the trend line may look collapsed."
                )
            hqc_targets = get_per_date_targets(selected, "High", hqc_raw_dates)
            has_targets = any(t is not None for t in hqc_targets)
            if has_targets:
                fallback_hqc_target = get_qc_target(selected, "High", as_of_date=max(hqc_raw_dates))
                fallback_hqc_mean = float(fallback_hqc_target["target_mean"]) if fallback_hqc_target else None
                fallback_hqc_sd = float(fallback_hqc_target["target_sd"]) if fallback_hqc_target else None

                hqc_means = [
                    float(t["target_mean"]) if t else fallback_hqc_mean
                    for t in hqc_targets
                ]
                hqc_sds = [
                    float(t["target_sd"]) if t else fallback_hqc_sd
                    for t in hqc_targets
                ]

                if any(v is None for v in hqc_means) or any(v is None for v in hqc_sds):
                    chart_cols[0].warning(
                        f"HQC target rows are missing for some dates of {selected}. "
                        "Import complete QC target table values (QC mean + SD) for this analyte."
                    )
                    hqc_means = None
                    hqc_sds = None
                    hqc_mean_val = None
                    hqc_sd = None
                else:
                    hqc_mean_val = hqc_means[-1]
                    hqc_sd = hqc_sds[-1]
                    hqc_target_mean = hqc_mean_val
                    hqc_target_sd = hqc_sd
                    chart_cols[0].caption("Chart lines use HQC summary-table targets (QC mean and SD bands) active per run date.")
            else:
                latest_hqc_target = get_qc_target(selected, "High", as_of_date=max(hqc_raw_dates))
                if latest_hqc_target:
                    hqc_mean_val = float(latest_hqc_target["target_mean"])
                    hqc_sd = float(latest_hqc_target["target_sd"])
                    hqc_means = [hqc_mean_val] * len(hqc_raw_dates)
                    hqc_sds = [hqc_sd] * len(hqc_raw_dates)
                    hqc_target_mean = hqc_mean_val
                    hqc_target_sd = hqc_sd
                    chart_cols[0].caption("Chart lines use stored HQC QC mean and SD target values.")
                else:
                    hqc_mean_val = None
                    hqc_sd = None
                    hqc_means = None
                    hqc_sds = None
                    chart_cols[0].warning(
                        f"No HQC QC target table values found for {selected}. "
                        "Chart reference lines require QC mean and SD from uploaded table."
                    )

            if hqc_sd is None or pd.isna(hqc_sd) or hqc_sd == 0:
                chart_cols[0].warning(f"HQC target SD is missing/invalid for {selected}.")
            else:
                hqc_fig = make_qc_chart(
                    hqc_dates, hqc_concentrations,
                    hqc_mean_val,
                    hqc_mean_val + 2 * hqc_sd,
                    hqc_mean_val - 2 * hqc_sd,
                    hqc_mean_val + 3 * hqc_sd,
                    hqc_mean_val - 3 * hqc_sd,
                    title=f"{selected} — HQC",
                    uploader_initials=hqc_data["uploaded_by"].fillna("NA").tolist(),
                    means_per_point=hqc_means,
                    sds_per_point=hqc_sds,
                )
                chart_cols[0].plotly_chart(hqc_fig, use_container_width=True)

        if lqc_data.empty:
            chart_cols[1].info(f"No LQC data for {selected}.")
        else:
            lqc_concentrations = lqc_data["concentration"].tolist()
            lqc_raw_dates = lqc_data["run_date"].tolist()
            lqc_dates = [d.replace("-", "/") for d in lqc_raw_dates]
            if len(set(lqc_raw_dates)) <= 1:
                chart_cols[1].warning(
                    f"LQC points for {selected} are from a single run date, so the trend line may look collapsed."
                )
            lqc_targets = get_per_date_targets(selected, "Low", lqc_raw_dates)
            has_lqc_targets = any(t is not None for t in lqc_targets)
            if has_lqc_targets:
                fallback_lqc_target = get_qc_target(selected, "Low", as_of_date=max(lqc_raw_dates))
                fallback_lqc_mean = float(fallback_lqc_target["target_mean"]) if fallback_lqc_target else None
                fallback_lqc_sd = float(fallback_lqc_target["target_sd"]) if fallback_lqc_target else None

                lqc_means = [
                    float(t["target_mean"]) if t else fallback_lqc_mean
                    for t in lqc_targets
                ]
                lqc_sds = [
                    float(t["target_sd"]) if t else fallback_lqc_sd
                    for t in lqc_targets
                ]

                if any(v is None for v in lqc_means) or any(v is None for v in lqc_sds):
                    chart_cols[1].warning(
                        f"LQC target rows are missing for some dates of {selected}. "
                        "Import complete QC target table values (QC mean + SD) for this analyte."
                    )
                    lqc_means = None
                    lqc_sds = None
                    lqc_mean_val = None
                    lqc_sd = None
                else:
                    lqc_mean_val = lqc_means[-1]
                    lqc_sd = lqc_sds[-1]
                    lqc_target_mean = lqc_mean_val
                    lqc_target_sd = lqc_sd
                    chart_cols[1].caption("Chart lines use LQC summary-table targets (QC mean and SD bands) active per run date.")
            else:
                latest_lqc_target = get_qc_target(selected, "Low", as_of_date=max(lqc_raw_dates))
                if latest_lqc_target:
                    lqc_mean_val = float(latest_lqc_target["target_mean"])
                    lqc_sd = float(latest_lqc_target["target_sd"])
                    lqc_means = [lqc_mean_val] * len(lqc_raw_dates)
                    lqc_sds = [lqc_sd] * len(lqc_raw_dates)
                    lqc_target_mean = lqc_mean_val
                    lqc_target_sd = lqc_sd
                    chart_cols[1].caption("Chart lines use stored LQC QC mean and SD target values.")
                else:
                    lqc_mean_val = None
                    lqc_sd = None
                    lqc_means = None
                    lqc_sds = None
                    chart_cols[1].warning(
                        f"No LQC QC target table values found for {selected}. "
                        "Chart reference lines require QC mean and SD from uploaded table."
                    )

            if lqc_sd is None or pd.isna(lqc_sd) or lqc_sd == 0:
                chart_cols[1].warning(f"LQC target SD is missing/invalid for {selected}.")
            else:
                lqc_fig = make_qc_chart(
                    lqc_dates, lqc_concentrations,
                    lqc_mean_val,
                    lqc_mean_val + 2 * lqc_sd,
                    lqc_mean_val - 2 * lqc_sd,
                    lqc_mean_val + 3 * lqc_sd,
                    lqc_mean_val - 3 * lqc_sd,
                    title=f"{selected} — LQC",
                    uploader_initials=lqc_data["uploaded_by"].fillna("NA").tolist(),
                    means_per_point=lqc_means,
                    sds_per_point=lqc_sds,
                )
                chart_cols[1].plotly_chart(lqc_fig, use_container_width=True)

        st.markdown("---")
        st.markdown("**HQC and LQC Statistics**")
        stats_cols = st.columns(2)

        if not hqc_data.empty:
            with stats_cols[0]:
                st.subheader("HQC Statistics")
                if hqc_target_mean is not None and hqc_target_sd is not None:
                    st.metric("HQC QC Mean (Target)", f"{hqc_target_mean:.4f}")
                    st.metric("HQC SD (Target)", f"{hqc_target_sd:.4f}")
                    st.metric("HQC +2SD", f"{(hqc_target_mean + 2 * hqc_target_sd):.4f}")
                    st.metric("HQC -2SD", f"{(hqc_target_mean - 2 * hqc_target_sd):.4f}")
                    st.metric("HQC +3SD", f"{(hqc_target_mean + 3 * hqc_target_sd):.4f}")
                    st.metric("HQC -3SD", f"{(hqc_target_mean - 3 * hqc_target_sd):.4f}")
                else:
                    st.metric("HQC QC Mean (Target)", "N/A")
                    st.metric("HQC SD (Target)", "N/A")
                st.metric("HQC Min", f"{hqc_data['concentration'].min():.4f}")
                st.metric("HQC Max", f"{hqc_data['concentration'].max():.4f}")

        if not lqc_data.empty:
            with stats_cols[1]:
                st.subheader("LQC Statistics")
                if lqc_target_mean is not None and lqc_target_sd is not None:
                    st.metric("LQC QC Mean (Target)", f"{lqc_target_mean:.4f}")
                    st.metric("LQC SD (Target)", f"{lqc_target_sd:.4f}")
                    st.metric("LQC +2SD", f"{(lqc_target_mean + 2 * lqc_target_sd):.4f}")
                    st.metric("LQC -2SD", f"{(lqc_target_mean - 2 * lqc_target_sd):.4f}")
                    st.metric("LQC +3SD", f"{(lqc_target_mean + 3 * lqc_target_sd):.4f}")
                    st.metric("LQC -3SD", f"{(lqc_target_mean - 3 * lqc_target_sd):.4f}")
                else:
                    st.metric("LQC QC Mean (Target)", "N/A")
                    st.metric("LQC SD (Target)", "N/A")
                st.metric("LQC Min", f"{lqc_data['concentration'].min():.4f}")
                st.metric("LQC Max", f"{lqc_data['concentration'].max():.4f}")

        st.caption("Select a different analyte from the sidebar list.")

    elif mode == "Report":
        st.header("📋 QC Final Report")
        
        if not DB_PATH.exists():
            st.error("Database not found. Import data first in the Database tab.")
            return
        
        report_df = generate_final_report()
        if report_df is None or report_df.empty:
            st.info("No QC data found in the database.")
            return
        
        st.markdown("**Summary of all analytes with LQC and HQC values**")
        
        # Display as a table with styling
        st.dataframe(
            report_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Analyte": st.column_config.TextColumn("Analyte", width="medium"),
                "Go to Dashboard": st.column_config.LinkColumn("Open Dashboard", width="small", display_text="Open"),
                "QC Level": st.column_config.TextColumn("QC Level", width="small"),
                "Pictogram": st.column_config.ImageColumn("Pictogram", width="medium"),
                "Recent": st.column_config.TextColumn("Recent Value", width="small"),
                "Mean": st.column_config.TextColumn("Mean", width="small"),
                "Min": st.column_config.TextColumn("Min", width="small"),
                "Max": st.column_config.TextColumn("Max", width="small"),
                "N": st.column_config.NumberColumn("N", width="tiny"),
                "Status": st.column_config.TextColumn("Status", width="medium"),
            }
        )
        
        st.markdown("---")
        st.markdown("**Export Report**")
        
        if st.button("📥 Export Report as CSV", use_container_width=True):
            export_df = report_df.drop(columns=["Pictogram"], errors="ignore")
            csv = export_df.to_csv(index=False)
            st.download_button(
                label="Download Report CSV",
                data=csv,
                file_name=f"QC_Report_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True
            )


if __name__ == "__main__":
    main()
