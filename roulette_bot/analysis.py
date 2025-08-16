from __future__ import annotations

from collections import Counter
from typing import Dict, List, Tuple

from .state import UserState


DOZEN_MAP = {
    1: "D1", 2: "D1", 3: "D1", 4: "D1", 5: "D1", 6: "D1", 7: "D1", 8: "D1", 9: "D1", 10: "D1", 11: "D1", 12: "D1",
    13: "D2", 14: "D2", 15: "D2", 16: "D2", 17: "D2", 18: "D2", 19: "D2", 20: "D2", 21: "D2", 22: "D2", 23: "D2", 24: "D2",
    25: "D3", 26: "D3", 27: "D3", 28: "D3", 29: "D3", 30: "D3", 31: "D3", 32: "D3", 33: "D3", 34: "D3", 35: "D3", 36: "D3",
}

def number_to_dozen(num: int) -> str:
    return DOZEN_MAP[num]

def validate_number(text: str) -> Tuple[bool, int | None]:
    """Valida número da roleta (0–36)."""
    try:
        n = int(text)
    except ValueError:
        return False, None
    if 0 <= n <= 36:
        return True, n
    return False, None


def _last_k_dozen(hist: List[int], k: int) -> List[str]:
    """Últimos k resultados (ignorando zeros) mapeados para dúzias."""
    dozens = [number_to_dozen(n) for n in hist if n != 0]
    return dozens[-k:]


def analyze(state: UserState) -> Dict[str, str]:
    """
    Estratégia: '3 em 4' + 1 Gale
      - Se houver gale pendente: força a mesma dúzia (1/1).
      - Caso contrário: só recomenda se uma dúzia saiu >= 3 vezes nos últimos 4 giros.
      - Senão: WAIT.
    """
    # Regras de espera por timers
    if state.cooldown_left > 0 or state.refractory_left > 0:
        return {"status": "wait"}

    hist = list(state.history)
    last12 = hist[-12:]

    # 1) Gale pendente? Força a mesma dúzia
    if state.gale_enabled and state.gale_left > 0 and state.gale_dozen:
        best = state.gale_dozen
        excluded = {"D1", "D2", "D3"} - {best}
        return {
            "status": "ok",
            "recommendation": best,
            "excluded": " + ".join(sorted(excluded)),
            "reason": "Gale 1/1 (reentrada controlada na mesma dúzia)",
            "history": ",".join(str(n) for n in last12),
            "pending": "0",
        }

    # 2) Sem gale: precisa de '3 em 4'
    tail4 = _last_k_dozen(hist, 4)
    if len(tail4) < 4:
        return {"status": "wait"}

    c4 = Counter(tail4)
    best, cnt = c4.most_common(1)[0]
    if cnt < 3:
        return {"status": "wait"}

    # recomendação single na dúzia dominante
    excluded = {"D1", "D2", "D3"} - {best}
    return {
        "status": "ok",
        "recommendation": best,
        "excluded": " + ".join(sorted(excluded)),
        "reason": "Padrão 3 em 4 (sequência validada)",
        "history": ",".join(str(n) for n in last12),
        "pending": "0",
    }
