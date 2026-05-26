from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OccupancyStatus(str, Enum):
    FULL = "FULL"
    CROWDED = "CROWDED"
    EMPTY = "EMPTY"


@dataclass(frozen=True)
class State:
    current_count: int
    status: OccupancyStatus
    updated_at: datetime
