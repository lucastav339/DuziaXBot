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

    # --- Modo conservador automático (gatilho por acurácia) ---
    conservative_boost: bool = False          # liga/desliga estratégia mais rígida
    acc_trigger: float = 0.50                 # limiar (≤ 50%)
    min_samples_for_eval: int = 8             # nº mínimo de jogadas para avaliar acurácia

    # --- Parâmetros da estratégia "apertada" ---
    use_ewma: bool = True                     # ponderar por recência
    ewma_alpha: float = 0.6                   # peso do recente
    min_gap: int = 2                          # gap mínimo (versão ponderada) quando boost ON
    min_support: int = 4                      # suporte mínimo na janela
    require_recent: int = 1                   # presença mínima nos últimos K giros
    # Controle de risco simples (opcional)
    max_loss_streak: int = 2                  # após X erros seguidos, faz cooldown
    cooldown_spins: int = 2                   # nº de giros sem recomendar
    cooldown_left: int = 0                    # contador de cooldown
    loss_streak: int = 0                      # erros consecutivos

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
        """Zera recomendação e placar (usado em /reset)."""
        self.current_rec = None
        self.rec_plays = 0
        self.rec_hits = 0
        self.rec_misses = 0
        self.loss_streak = 0
        self.cooldown_left = 0

    def set_recommendation(self, dozens: Set[str]) -> None:
        """Atualiza a recomendação ativa SEM zerar placar (cumulativo)."""
        self.current_rec = set(dozens) if dozens else None
