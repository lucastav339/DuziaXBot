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
    """Validate a numeric string within roulette range."""
    try:
        n = int(text)
    except ValueError:
        return False, None
    if 0 <= n <= 36:
        return True, n
    return False, None


def analyze(state: UserState) -> Dict[str, str]:
    hist = list(state.history)
    window = state.window
    recent = hist[-window:]
    last12 = hist[-12:]
    result: Dict[str, str] = {}

    if not hist or len(hist) < 5:
        result["status"] = "wait"
        return result

    # Frequency counts
    dozens = [number_to_dozen(n) for n in recent if n != 0]
    freq = Counter(dozens)
    # Determine top frequencies
    if not freq:
        result["status"] = "wait"
        return result

    top = freq.most_common()
    top1, top1c = top[0]
    top3c = top[-1][1]
    recommendation: List[str] = []

    # Check runs
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

    gap_check = []
    if top1c - top3c >= 1 and top1c >= 4:
        gap_check.append(top1)
        if len(top) > 1 and top[1][1] + 1 == top1c and top[1][0] in dozens[-3:]:
            gap_check.append(top[1][0])

    # Presence recent
    valid_dozen = set()
    last3 = dozens[-3:]
    for d in {d for d, _ in top[:2]}:
        if d in last3:
            valid_dozen.add(d)

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

    # Apply mode rules
    mode = state.mode
    if mode == "conservador":
        candidate = set(runs_check or gap_check)
        candidate &= valid_dozen if valid_dozen else candidate
        if not candidate and anti_overfit:
            candidate = set(anti_overfit)
    elif mode == "agressivo":
        candidate = set(runs_check)
        if not candidate and top1c - (top[1][1] if len(top) > 1 else 0) >= 2:
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
    # Build justification message
    if runs_check:
        result["reason"] = "Dominância recente"
    elif gap_check:
        result["reason"] = "Gap de frequência"
    else:
        result["reason"] = "Heurística"

    result["history"] = ",".join(str(n) for n in last12)
    result["pending"] = str(max(0, window - len(recent)))
    return result
