from __future__ import annotations

import math
from collections import Counter
from typing import Dict, List, Tuple

from .state import UserState


DOZEN_MAP = {
    1: "D1", 2: "D1", 3: "D1", 4: "D1", 5: "D1", 6: "D1", 7: "D1", 8: "D1", 9: "D1", 10: "D1", 11: "D1", 12: "D1",
    13: "D2", 14: "D2", 15: "D2", 16: "D2", 17: "D2", 18: "D2", 19: "D2", 20: "D2", 21: "D2", 22: "D2", 23: "D2", 24: "D2",
    25: "D3", 26: "D3", 27: "D3", 28: "D3", 29: "D3", 30: "D3", 31: "D3", 32: "D3", 33: "D3", 34: "D3", 35: "D3", 36: "D3",
}
BASE_P = 12 / 37.0  # probabilidade base de uma dúzia na roleta europeia (~0.3243)


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


def _ewma_freq(dozens: List[str], alpha: float) -> Dict[str, float]:
    """EWMA simples (mais recentes pesam mais)."""
    acc = {"D1": 0.0, "D2": 0.0, "D3": 0.0}
    for d in dozens:
        for k in acc:
            acc[k] *= (1.0 - alpha)
        acc[d] += alpha
    return acc


def _dirichlet_post_probs(counts: Dict[str, int], alpha0: float) -> Dict[str, float]:
    alphas = {d: alpha0 + counts.get(d, 0) for d in ("D1", "D2", "D3")}
    s = sum(alphas.values())
    return {d: alphas[d] / s for d in alphas}


def _beta_lower_bound(successes: int, trials: int, z: float = 1.2816) -> float:
    """Limite inferior Wilson-like para p, aproxima IC 90% (z≈1.2816)."""
    if trials <= 0:
        return 0.0
    p = successes / trials
    denom = 1.0 + (z * z) / trials
    center = p + (z * z) / (2 * trials)
    rad = z * math.sqrt(p * (1 - p) / trials + (z * z) / (4 * trials * trials))
    return max(0.0, (center - rad) / denom)


def _run_length_end(dozens_all: List[str]) -> Tuple[str | None, int]:
    """Retorna (dúzia, comprimento) da sequência no final do histórico."""
    if not dozens_all:
        return None, 0
    last = dozens_all[-1]
    i = len(dozens_all) - 1
    cnt = 0
    while i >= 0 and dozens_all[i] == last:
        cnt += 1
        i -= 1
    return last, cnt


def analyze(state: UserState) -> Dict[str, str]:
    """
    Estratégia SINGLE-DOZEN:
      - Prioriza a dúzia com maior evidência (SPRT forte > Bayes + EWMA + suporte/presença).
      - Só retorna 1 dúzia; caso não haja evidência suficiente, retorna 'wait'.
    """
    # Cooldown ativo → não recomenda
    if state.cooldown_left > 0:
        return {"status": "wait"}

    hist = list(state.history)
    window = state.window
    recent = hist[-window:]
    last12 = hist[-12:]

    if len(hist) < 5:
        return {"status": "wait"}

    dozens_recent = [number_to_dozen(n) for n in recent if n != 0]
    if not dozens_recent:
        return {"status": "wait"}

    counts = Counter(dozens_recent)
    trials = len(dozens_recent)

    # EWMA (ou fallback p/ contagem)
    if state.use_ewma:
        wf = _ewma_freq(dozens_recent, state.ewma_alpha)
    else:
        wf = {d: float(counts.get(d, 0)) for d in ("D1", "D2", "D3")}

    # Bayes: posterior + limite inferior
    probs = _dirichlet_post_probs(counts, alpha0=1.0)  # prior fraca padrão
    z = 1.2816 if state.bayes_ci_q <= 0.10 else 1.6449  # 90% ou 95%
    lb = {d: _beta_lower_bound(counts.get(d, 0), trials, z=z) for d in ("D1", "D2", "D3")}
    lift = {d: probs[d] - BASE_P for d in ("D1", "D2", "D3")}

    # Runs no final (reforço de confiança)
    dozens_all = [number_to_dozen(n) for n in hist if n != 0]
    end_d, end_run = _run_length_end(dozens_all)

    # Ranking de candidatos (combinação simples de sinais)
    def score(d: str) -> float:
        # Normalizações
        ew = wf[d]
        ew_max = max(wf.values()) if wf else 1.0
        ew_n = (ew / ew_max) if ew_max > 0 else 0.0
        sprt_n = (state.llr[d] - state.sprt_B) / (state.sprt_A - state.sprt_B)
        sprt_n = max(0.0, min(1.0, sprt_n))
        support_n = counts.get(d, 0) / max(1, trials)
        run_n = 1.0 if (d == end_d and end_run >= 2) else 0.0
        lift_n = max(0.0, lift[d] / 0.2)  # escala aproximada

        # Pesos (pode calibrar depois)
        w_sprt, w_bayes, w_ewma, w_support, w_run = 1.5, 1.2, 1.0, 0.7, 0.8
        lin = (w_sprt * sprt_n) + (w_bayes * lift_n) + (w_ewma * ew_n) + (w_support * support_n) + (w_run * run_n)
        return lin

    ranked = sorted(("D1", "D2", "D3"), key=score, reverse=True)
    best = ranked[0]

    # GATES (sempre single)
    presence_ok = dozens_recent[-state.require_recent :].count(best) >= 1 if state.require_recent > 0 else True
    support_ok = counts.get(best, 0) >= state.min_support
    bayes_ok = (lift[best] >= state.bayes_lift_min) and (lb[best] > BASE_P)
    sprt_strong = state.llr[best] >= state.sprt_A
    runs_strong = (best == end_d and end_run >= 3)

    if state.conservative_boost:
        # Conservador: exige pelo menos SPRT forte OU (Bayes + suporte + presença)
        if not (sprt_strong or (bayes_ok and support_ok and presence_ok)):
            return {"status": "wait"}
    else:
        # Normal: exige pelo menos (SPRT forte) OU (runs fortes) OU (Bayes + suporte + presença)
        if not (sprt_strong or runs_strong or (bayes_ok and support_ok and presence_ok)):
            return {"status": "wait"}

    # Monta resposta (sempre 1 dúzia)
    rec_set = {best}
    rec = next(iter(rec_set))
    excluded = {"D1", "D2", "D3"} - rec_set
    reason = (
        "SPRT forte"
        if sprt_strong
        else ("Sequência dominante" if runs_strong else "Bayes + suporte + presença (EWMA)")
    )

    return {
        "status": "ok",
        "recommendation": rec,  # <- apenas 1 dúzia
        "excluded": " + ".join(sorted(excluded)) if excluded else "",
        "reason": reason,
        "history": ",".join(str(n) for n in last12),
        "pending": str(max(0, window - len(recent))),
    }
