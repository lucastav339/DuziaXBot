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
    try:
        n = int(text)
    except ValueError:
        return False, None
    if 0 <= n <= 36:
        return True, n
    return False, None

def _ewma_freq(dozens: List[str], alpha: float) -> Dict[str, float]:
    """EWMA: mais recentes pesam mais."""
    acc = {"D1": 0.0, "D2": 0.0, "D3": 0.0}
    for d in dozens:
        for k in acc:
            acc[k] *= (1.0 - alpha)
        acc[d] += alpha
    return acc

def _choose_single_from_gap_and_filters(
    dozens_recent: List[str],
    ewma_alpha: float,
    gap_threshold: float,
    min_support: int,
    require_recent: int,
) -> Tuple[str | None, Dict[str, float], Counter]:
    """
    Retorna (dozen_escolhida | None, wf, raw)
    - wf: pesos EWMA
    - raw: contagem bruta na janela
    """
    if not dozens_recent:
        return None, {}, Counter()

    raw = Counter(dozens_recent)
    wf = _ewma_freq(dozens_recent, ewma_alpha)

    ordered = sorted(wf.items(), key=lambda x: x[1], reverse=True)
    top1, top1w = ordered[0]
    top2w = ordered[1][1] if len(ordered) > 1 else 0.0

    gap_ok = (top1w - top2w) >= gap_threshold
    support_ok = raw[top1] >= min_support
    last3 = dozens_recent[-3:]
    presence_ok = last3.count(top1) >= require_recent

    if gap_ok and support_ok and presence_ok:
        return top1, wf, raw
    return None, wf, raw

def _analyze_conservative(state: UserState, hist: List[int]) -> Dict[str, str]:
    """Estratégia apertada (ativa quando conservative_boost=True). Sempre SINGLE."""
    window = state.window
    recent = hist[-window:]
    last12 = hist[-12:]
    result: Dict[str, str] = {}

    if not hist or len(hist) < 5:
        result["status"] = "wait"
        return result

    dozens_recent = [number_to_dozen(n) for n in recent if n != 0]
    if not dozens_recent:
        result["status"] = "wait"
        return result

    # Runs (3 últimas iguais) -> single
    last_dozen_all = [number_to_dozen(n) for n in hist if n != 0]
    runs_ok = len(last_dozen_all) >= 3 and last_dozen_all[-3:] == [last_dozen_all[-1]] * 3

    if runs_ok:
        choice = last_dozen_all[-1]
        last3 = dozens_recent[-3:]
        if last3.count(choice) >= max(1, state.require_recent):
            rec = choice
            excluded = {"D1", "D2", "D3"} - {rec}
            return {
                "status": "ok",
                "recommendation": rec,
                "excluded": " + ".join(sorted(excluded)),
                "reason": "Dominância por sequência",
                "history": ",".join(str(n) for n in last12),
                "pending": str(max(0, window - len(recent))),
            }

    # EWMA + filtros (mais rígido no boost)
    choice, wf, raw = _choose_single_from_gap_and_filters(
        dozens_recent=dozens_recent,
        ewma_alpha=state.ewma_alpha,
        gap_threshold=state.min_gap_wf_boost,
        min_support=state.min_support,
        require_recent=state.require_recent,
    )
    if not choice:
        return {"status": "wait"}

    rec = choice
    excluded = {"D1", "D2", "D3"} - {rec}
    return {
        "status": "ok",
        "recommendation": rec,
        "excluded": " + ".join(sorted(excluded)),
        "reason": "Gap de frequência + suporte",
        "history": ",".join(str(n) for n in last12),
        "pending": str(max(0, window - len(recent))),
    }

def _analyze_original(state: UserState, hist: List[int]) -> Dict[str, str]:
    """Estratégia normal (sempre SINGLE): prioridade RUN forte, senão EWMA+gap."""
    window = state.window
    recent = hist[-window:]
    last12 = hist[-12:]
    result: Dict[str, str] = {}

    if not hist or len(hist) < 5:
        result["status"] = "wait"
        return result

    dozens_recent = [number_to_dozen(n) for n in recent if n != 0]
    if not dozens_recent:
        result["status"] = "wait"
        return result

    # 1) RUN forte (3 últimas iguais OU 3 em 4 com as 2 últimas iguais)
    last_dozen = [number_to_dozen(n) for n in hist if n != 0]
    run_choice: str | None = None
    if len(last_dozen) >= 3 and last_dozen[-3:] == [last_dozen[-1]] * 3:
        run_choice = last_dozen[-1]
    elif len(last_dozen) >= 4:
        last4 = last_dozen[-4:]
        counts4 = Counter(last4)
        for d, c in counts4.items():
            if c >= 3 and last4[-1] == last4[-2] == d:
                run_choice = d
                break

    if run_choice:
        # validar presença recente
        last3 = dozens_recent[-3:]
        if last3.count(run_choice) >= max(1, state.require_recent):
            rec = run_choice
            excluded = {"D1", "D2", "D3"} - {rec}
            return {
                "status": "ok",
                "recommendation": rec,
                "excluded": " + ".join(sorted(excluded)),
                "reason": "Dominância recente",
                "history": ",".join(str(n) for n in last12),
                "pending": str(max(0, window - len(recent))),
            }

    # 2) EWMA + gap (normal)
    choice, wf, raw = _choose_single_from_gap_and_filters(
        dozens_recent=dozens_recent,
        ewma_alpha=state.ewma_alpha,
        gap_threshold=state.min_gap_wf_normal,
        min_support=state.min_support,
        require_recent=state.require_recent,
    )
    if not choice:
        return {"status": "wait"}

    # Anti-overfit single (domina muito os últimos 10)
    last10 = [number_to_dozen(n) for n in hist[-10:] if n != 0]
    counts10 = Counter(last10)
    if counts10:
        dom, domc = counts10.most_common(1)[0]
        others = {d: c for d, c in counts10.items() if d != dom}
        if domc >= 5 and all(c < 3 for c in others.values()):
            # mantém single no dominante
            choice = dom

    rec = choice
    excluded = {"D1", "D2", "D3"} - {rec}
    return {
        "status": "ok",
        "recommendation": rec,
        "excluded": " + ".join(sorted(excluded)),
        "reason": "Gap de frequência + suporte" if not run_choice else "Dominância recente",
        "history": ",".join(str(n) for n in last12),
        "pending": str(max(0, window - len(recent))),
    }

def analyze(state: UserState) -> Dict[str, str]:
    """Seleciona análise conforme boost conservador."""
    hist = list(state.history)
    if state.conservative_boost:
        return _analyze_conservative(state, hist)
    return _analyze_original(state, hist)
