from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class FileStreamRef:
    pid: str
    dsid: str
    label: str
    stream_type: str  # "primary_pdf" or "supplemental"
    size: int
    sha256: str
    md5: str
    text: str = ""
    match_method: str = "exact_sha256"


@dataclass
class DuplicateGroup:
    match_key: str
    size: int
    match_type: str  # "exact_sha256" or "text_fuzzy"
    streams: list[FileStreamRef] = field(default_factory=list)
    is_cross_pid: bool = False
    pids: set[str] = field(default_factory=set)