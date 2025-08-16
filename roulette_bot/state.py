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
    current_rec: Optional[Set[str]] = None   # ex.: {"D1"} (sempre single nesta versão)
    rec_plays: int = 0
    rec_hits: int = 0
    rec_misses: int = 0

    # --- Modo conservador automático (gatilho por acurácia) ---
    conservative_boost: bool = False
    acc_trigger: float = 0.50
    min_samples_for_eval: int = 8

    # --- Parâmetros de estratégia (single-dozen) ---
    use_ewma: bool = True
    ewma_alpha: float = 0.6
    min_support: int = 4          # ocorrências mínimas do candidato na janela
    require_recent: int = 1       # presença nos últimos K giros (>=)
    bayes_lift_min: float = 0.03  # p_posterior - base >= 3 p.p.
    bayes_ci_q: float = 0.10      # IC ~90% (limite inferior > base)

    # --- SPRT (detecção de viés real) ---
    sprt_delta: float = 0.05      # H1 = base + 5 p.p.
    sprt_A: float = 2.30          # limiar superior (≈ BF 10:1)
    sprt_B: float = -2.30         # limiar inferior
    llr: dict = field(default_factory=lambda: {"D1": 0.0, "D2": 0.0, "D3": 0.0})

    # --- CUSUM (mudança de regime) ---
    cusum: dict = field(default_factory=lambda: {"D1": 0.0, "D2": 0.0, "D3": 0.0})
    cusum_k: float = 0.01
    cusum_h: float = 0.20

    # --- Controle de risco (opcional, já integrado ao fluxo) ---
    max_loss_streak: int = 2
    cooldown_spins: int = 2
    cooldown_left: int = 0
    loss_streak: int = 0

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
        # Mantemos LLR/CUSUM para continuidade; se quiser, zere-os aqui também.

    def set_recommendation(self, dozens: Set[str]) -> None:
        """Atualiza a recomendação ativa SEM zerar placar (cumulativo)."""
        self.current_rec = set(dozens) if dozens else None
