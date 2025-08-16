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
    """EWMA simples: mais recentes pesam mais."""
    acc = {"D1": 0.0, "D2": 0.0, "D3": 0.0}
    for d in dozens:
        for k in acc:
            acc[k] *= (1.0 - alpha)
        acc[d] += alpha
    return acc


def _analyze_conservative(state: UserState, hist: List[int]) -> Dict[str, str]:
    """Estratégia apertada (ativa quando conservative_boost=True)."""
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

    # Frequência ponderada por recência
    wf = _ewma_freq(dozens_recent, state.ewma_alpha) if state.use_ewma else None
    raw = Counter(dozens_recent)

    if wf:
        top = sorted(wf.items(), key=lambda kv: kv[1], reverse=True)
        top1, top1w = top[0]
        top2w = top[1][1] if len(top) > 1 else 0.0
        top1c = raw[top1]
        top2c = raw[top[1][0]] if len(top) > 1 else 0
        gap_ok = (top1w - top2w) >= float(state.min_gap)
    else:
        top_counts = raw.most_common()
        top1, top1c = top_counts[0]
        top2c = top_counts[1][1] if len(top_counts) > 1 else 0
        gap_ok = (top1c - top2c) >= state.min_gap

    # Presença recente
    last3 = dozens_recent[-3:]
    presence_ok = last3.count(top1) >= state.require_recent

    # Suporte mínimo
    support_ok = raw[top1] >= state.min_support

    # Runs (3 últimas iguais) reforça single-dozen
    last_dozen_all = [number_to_dozen(n) for n in hist if n != 0]
    runs_ok = len(last_dozen_all) >= 3 and last_dozen_all[-3:] == [last_dozen_all[-1]] * 3

    if runs_ok and presence_ok:
        candidate = {top1}
        reason = "Dominância por sequência"
    elif gap_ok and support_ok and presence_ok:
        candidate = {top1}
        reason = "Gap de frequência + suporte"
        # Se o segundo apareceu nos últimos 3, permitir dupla defensiva
        for dz in reversed(last3):
            if dz != top1:
                candidate.add(dz)
                break
    else:
        return {"status": "wait"}

    rec = " + ".join(sorted(candidate))
    excluded = {"D1", "D2", "D3"} - set(candidate)
    return {
        "status": "ok",
        "recommendation": rec,
        "excluded": " + ".join(sorted(excluded)) if excluded else "",
        "reason": reason,
        "history": ",".join(str(n) for n in last12),
        "pending": str(max(0, window - len(recent))),
    }


def _analyze_original(state: UserState, hist: List[int]) -> Dict[str, str]:
    """Aproxima a sua análise original (com pequenas proteções)."""
    window = state.window
    recent = hist[-window:]
    last12 = hist[-12:]
    result: Dict[str, str] = {}

    if not hist or len(hist) < 5:
        result["status"] = "wait"
        return result

    dozens = [number_to_dozen(n) for n in recent if n != 0]
    if not dozens:
        result["status"] = "wait"
        return result

    freq = Counter(dozens)
    if not freq:
        result["status"] = "wait"
        return result

    top = freq.most_common()
    top1, top1c = top[0]
    top3c = top[-1][1]
    recommendation: List[str] = []

    # Runs
    last_dozen = [number_to_dozen(n) for n in hist if n != 0]
    runs_check = []
    if len(last_dozen) >= 4:
        last4 = last_dozen[-4:]
        counts = Counter(last4)
        for d, c in counts.items():
            if c >= 3 and last4[-1] == d and last4[-2] == d:
                runs_check.append(d)
        if len(last_dozen) >= 3 and last_dozen[-3:] == [last_dozen[-1]] * 3:
            runs_check.append(last_dozen[-1])
    runs_check = list(set(runs_check))

    # Gap
    gap_check = []
    if top1c - top3c >= 1 and top1c >= 4:
        gap_check.append(top1)
        if len(top) > 1 and top[1][1] + 1 == top1c and top[1][0] in dozens[-3:]:
            gap_check.append(top[1][0])

    # Presença recente top2
    valid_dozen = set()
    last3 = dozens[-3:]
    for d, _ in top[:2]:
        if d in last3:
            valid_dozen.add(d)

    # Anti-overfit (como no seu)
    anti_overfit = []
    last10 = [number_to_dozen(n) for n in hist[-10:] if n != 0]
    counts10 = Counter(last10)
    if counts10:
        dom, domc = counts10.most_common(1)[0]
        others = {d: c for d, c in counts10.items() if d != dom}
        if domc >= 5 and all(c < 3 for c in others.values()):
            if others:
                co = max(others, key=others.get)
                anti_overfit = [dom, co]

    mode = state.mode
    if mode == "conservador":
        candidate = set(runs_check or gap_check)
        candidate &= valid_dozen if valid_dozen else candidate
        if not candidate and anti_overfit:
            candidate = set(anti_overfit)
    elif mode == "agressivo":
        candidate = set(runs_check)
        if not candidate and len(top) > 1 and (top1c - top[1][1]) >= 2:
            candidate = {top1}
    else:  # neutro
        candidate = set(gap_check)
        candidate &= valid_dozen if valid_dozen else candidate

    if not candidate:
        result["status"] = "wait"
        return result

    rec = " + ".join(sorted(candidate))
    excluded = {"D1", "D2", "D3"} - set(candidate)
    result["status"] = "ok"
    result["recommendation"] = rec
    result["excluded"] = " + ".join(sorted(excluded)) if excluded else ""
    result["reason"] = "Dominância recente" if runs_check else ("Gap de frequência" if gap_check else "Heurística")
    result["history"] = ",".join(str(n) for n in last12)
    result["pending"] = str(max(0, window - len(recent)))
    return result


def analyze(state: UserState) -> Dict[str, str]:
    """Seleciona análise conforme boost conservador."""
    hist = list(state.history)
    if state.conservative_boost:
        return _analyze_conservative(state, hist)
    return _analyze_original(state, hist)
