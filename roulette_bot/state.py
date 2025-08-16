from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from typing import Deque, Optional, Set


@dataclass
class UserState:
    """Estado por usuário do bot."""

    # --- Histórico ---
    history: Deque[int] = field(default_factory=lambda: deque(maxlen=100))
    window: int = 12  # só para exibição de últimos 12 no layout antigo

    # --- Flags gerais (mantém compatibilidade com seu design) ---
    explain_next: bool = False
    mode: str = "conservador"   # se seu formato mostra o modo, mantemos o campo

    # --- Stake (opcional; apenas exibição se seu design usa) ---
    stake_on: bool = False
    stake_value: float = 1.0
    progression: Optional[str] = None  # "martingale" | "dalembert" | None

    # --- Recomendação atual (sempre 1 dúzia) ---
    current_rec: Optional[Set[str]] = None   # {"D1"} | None
    rec_active: bool = False                 # TRUE se a ÚLTIMA resposta foi recomendação (não WAIT)

    # --- Placar cumulativo (seu design já exibe isso) ---
    rec_plays: int = 0
    rec_hits: int = 0
    rec_misses: int = 0

    # --- Gale (somente 1 tentativa) ---
    gale_enabled: bool = True
    gale_max: int = 1
    gale_left: int = 0               # 0 = sem gale pendente; 1 = 1 gale a fazer
    gale_dozen: Optional[str] = None # alvo (D1/D2/D3)
    gale_recover_miss: bool = False  # se True, erro que disparou o gale será anulado se o gale acertar

    # --- Pausas simples (compatível com seu fluxo, se quiser usar) ---
    refractory_spins: int = 0
    refractory_left: int = 0
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
        """Zera SOMENTE recomendação + placar (use no /reset)."""
        self.current_rec = None
        self.rec_active = False
        self.rec_plays = 0
        self.rec_hits = 0
        self.rec_misses = 0
        # zera gale e timers
        self.gale_left = 0
        self.gale_dozen = None
        self.gale_recover_miss = False
        self.refractory_left = 0
        self.cooldown_left = 0

    def set_recommendation(self, dozen: str | None) -> None:
        """Define a recomendação ativa (sempre 1 dúzia) SEM zerar placar."""
        self.current_rec = {dozen} if dozen else None
