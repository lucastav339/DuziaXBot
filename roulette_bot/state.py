from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional, Set


@dataclass
class UserState:
    """State information for a single user."""

    history: Deque[int] = field(default_factory=lambda: deque(maxlen=100))
    mode: str = "conservador"
    window: int = 12
    stake_on: bool = False
    stake_value: float = 1.0
    progression: Optional[str] = None  # "martingale" or "dalembert"
    explain_next: bool = False

    # --- Placar cumulativo da recomendação ---
    current_rec: Optional[Set[str]] = None   # ex.: {"D1","D2"}
    rec_plays: int = 0
    rec_hits: int = 0
    rec_misses: int = 0

    def reset_history(self) -> None:
        self.history.clear()

    def add_number(self, num: int) -> None:
        self.history.append(num)

    def correct_last(self, num: int) -> bool:
        if not self.history:
            return False
        self.history[-1] = num
        return True

    # Zera o placar e a recomendação ativa
    def clear_recommendation(self) -> None:
        self.current_rec = None
        self.rec_plays = 0
        self.rec_hits = 0
        self.rec_misses = 0

    # Atualiza a recomendação ativa SEM zerar o placar (cumulativo)
    def set_recommendation(self, dozens: Set[str]) -> None:
        self.current_rec = set(dozens) if dozens else None
