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