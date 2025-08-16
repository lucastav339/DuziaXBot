from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional


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

    def reset_history(self) -> None:
        self.history.clear()

    def add_number(self, num: int) -> None:
        self.history.append(num)

    def correct_last(self, num: int) -> bool:
        if not self.history:
            return False
        self.history[-1] = num
        return True
