from typing import List, NamedTuple
from uuid import UUID
from enum import Enum


class ElectionType(Enum):
    plurality = 1
    range = 2
    approval = 3
    veto = 4
    borda = 5


class Election(NamedTuple):
    election_id: UUID
    selected_election_type: ElectionType
    candidates: List[str]
    winner_count: int
    p: int
    L: int

__all__ = ['ElectionType', 'Election']