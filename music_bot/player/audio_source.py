from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioSource:
    file_path: Optional[str] = None
    url: Optional[str] = None
    title: str = ""
    performer: str = ""
