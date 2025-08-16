from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional, Set


@dataclass
class UserState:
    """Estado por usuário do bot."""

    # --- Histórico ---
    history: Deque[int] = field(default_factory=lambda: deque(maxlen=100))
    window: int = 12  # ainda mostramos últimos 12 na mensagem

    # --- Modo / flags gerais ---
    explain_next: bool = False

    # --- Stake (opcional; só exibição) ---
    stake_on: bool = False
    stake_value: float = 1.0
    progression: Optional[str] = None  # "martingale" ou "dalembert" (não usado nesta estratégia)

    # --- Recomendação atual (sempre 1 dúzia) ---
    current_rec: Optional[Set[str]] = None   # ex.: {"D1"}
    rec_active: bool = False                 # TRUE se a ÚLTIMA resposta foi recomendação (não WAIT)

    # --- Placar cumulativo ---
    rec_plays: int = 0
    rec_hits: int = 0
    rec_misses: int = 0

    # --- Gale (somente 1 tentativa) ---
    gale_enabled: bool = True
    gale_max: int = 1
    gale_left: int = 0               # 0 = sem gale pendente; 1 = 1 gale a fazer
    gale_dozen: Optional[str] = None # dúzia alvo do gale (D1/D2/D3)

    # --- Pós-erro (opcional simples) ---
    refractory_spins: int = 0        # se quiser “pausar” após perder o gale; 0 = desligado
    refractory_left: int = 0

    # --- Cooldown simples (não usado por padrão aqui) ---
    cooldown_spins: int = 0
    cooldown_left: int = 0

    def reset_history(self) -> None:
        self.history.clear()

    def add_number(self, num: int) -> None:
        self.history.append(num)

    def correct_last(self, num: int) -> bool:
        if not self.history:
            return False
        self.history[-1] = num
        return True

    def clear_recommendation(self) -> None:
        """Zera SOMENTE a recomendação e o placar (use no /reset)."""
        self.current_rec = None
        self.rec_active = False
        self.rec_plays = 0
        self.rec_hits = 0
        self.rec_misses = 0
        # zera gale e timers
        self.gale_left = 0
        self.gale_dozen = None
        self.refractory_left = 0
        self.cooldown_left = 0

    def set_recommendation(self, dozen: str | None) -> None:
        """Define a recomendação ativa (sempre 1 dúzia) SEM zerar placar."""
        self.current_rec = {dozen} if dozen else None
