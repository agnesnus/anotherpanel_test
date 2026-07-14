from dataclasses import dataclass
from typing import Optional

@dataclass
class SampleInfo:
    data_filename: str
    sample_type: str
    calibrator_level: Optional[str] = None
    qc_level: Optional[str] = None
    qc_replicate: Optional[int] = None
    collection_date: Optional[str] = None
    patient_sequence: Optional[str] = None
    eqa_scheme: Optional[str] = None
    eqa_year: Optional[int] = None
    eqa_round: Optional[int] = None
    eqa_sample_code: Optional[str] = None
    eqa_replicate: Optional[str] = None