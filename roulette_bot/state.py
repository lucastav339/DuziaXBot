# state.py
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
    current_rec: Optional[Set[str]] = None   # ex.: {"D2","D3"} (invertida)
    rec_plays: int = 0
    rec_hits: int = 0
    rec_misses: int = 0

    # --- Modo conservador automático (gatilho por acurácia) ---
    conservative_boost: bool = False          # liga/desliga estratégia mais rígida
    acc_trigger: float = 0.50                 # limiar (≤ 50%)
    min_samples_for_eval: int = 12            # nº mínimo de jogadas para avaliar acurácia

    # --- Parâmetros da estratégia (ajuste fino) ---
    use_ewma: bool = True                     # ponderar por recência
    ewma_alpha: float = 0.60                  # peso do recente
    min_support: int = 4                      # suporte mínimo na janela
    require_recent: int = 1                   # presença mínima nos últimos K giros
    min_gap_wf_normal: float = 0.10           # gap ponderado (normal)
    min_gap_wf_boost: float = 0.12            # gap ponderado (boost)

    # Controle de risco simples
    max_loss_streak: int = 2                  # após X erros seguidos, faz cooldown
    cooldown_spins: int = 2                   # nº de giros sem recomendar
    cooldown_left: int = 0                    # contador de cooldown
    loss_streak: int = 0                      # erros consecutivos

    # Ritmo: no máx. 1 entrada a cada 2 giros (padrão)
    min_spins_between_entries: int = 2
    spin_count: int = 0
    last_entry_spin: int = -10**9

    # Aposta aberta? (controla quando contar o próximo resultado)
    has_open_rec: bool = False

    # --- NOVO: Ciclo pós-acerto (coleta 5, analisa, zera, repete) ---
    post_win_mode_active: bool = False
    post_win_wait_left: int = 0               # quantos giros ainda faltam coletar

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
        self.has_open_rec = False
        # também desligamos o ciclo pós-acerto
        self.post_win_mode_active = False
        self.post_win_wait_left = 0

    def set_recommendation(self, dozens: Set[str]) -> None:
        """Atualiza a recomendação ativa SEM zerar placar (cumulativo)."""
        self.current_rec = set(dozens) if dozens else None

    # --- helpers do ciclo pós-acerto ---
    def start_post_win_cycle(self, collect_n: int = 5) -> None:
        self.post_win_mode_active = True
        self.post_win_wait_left = max(0, int(collect_n))

    def step_post_win_collect(self) -> bool:
        """
        Decrementa contador de coleta se ativo.
        Retorna True se ainda estamos coletando (ou seja, deve apenas 'aguardar').
        """
        if self.post_win_mode_active and self.post_win_wait_left > 0:
            self.post_win_wait_left -= 1
            return True
        return False

    def restart_post_win_collect(self, collect_n: int = 5) -> None:
        """Reinicia o ciclo de coleta pós-acerto para manter 'sempre assim'."""
        self.post_win_mode_active = True
        self.post_win_wait_left = max(0, int(collect_n))
