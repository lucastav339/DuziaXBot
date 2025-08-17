# --- Modo conservador automático (gatilho por acurácia) ---
conservative_boost: bool = False
acc_trigger: float = 0.50
min_samples_for_eval: int = 12   # (ajuste fino: 12 em vez de 8)

# --- Parâmetros da estratégia (normal/boost) ---
use_ewma: bool = True
ewma_alpha: float = 0.60         # (ajuste fino)
min_support: int = 4             # (suporte mínimo na janela)
require_recent: int = 1          # (aparecer em >=1 dos últimos 3)

# Gap mínimo quando usa EWMA (fração 0..1)
min_gap_wf_normal: float = 0.10  # (normal)
min_gap_wf_boost: float = 0.12   # (boost mais rígido)

# Controle de risco simples
max_loss_streak: int = 2
cooldown_spins: int = 2
cooldown_left: int = 0
loss_streak: int = 0

# Ritmo: no máx. 1 entrada a cada 2 giros
min_spins_between_entries: int = 2
spin_count: int = 0
last_entry_spin: int = -10**9
