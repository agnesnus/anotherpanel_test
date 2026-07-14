from pathlib import Path
import os

# Keep DB inside this repo folder, regardless of launch directory
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("QC_STUDIO_DB_PATH", str(REPO_ROOT / "test_panel.db"))).resolve()

SAMPLE_TYPES = [
    {"type_code": "calibrator", "description": "Calibration standards (Cal 0 through Cal F)"},
    {"type_code": "qc", "description": "Quality control samples (Low/High)"},
    {"type_code": "patient", "description": "Patient specimens"},
    {"type_code": "eqa", "description": "External quality assessment / proficiency testing"},
    {"type_code": "blank", "description": "Solvent blanks"},
    {"type_code": "process_blank", "description": "Process/extraction blanks"},
]