# main.py
import os
import re
import logging
import secrets
from html import escape as esc
from typing import Dict, Any, List, Tuple

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -----------------------------------------------------------------------------
# LOGGING + BUILD TAG
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("roulette-bot")
BUILD_TAG = os.getenv("RENDER_GIT_COMMIT") or "local"
log.info("Starting build: %s", BUILD_TAG)

# -----------------------------------------------------------------------------
# CONFIG / CONSTANTES
# -----------------------------------------------------------------------------
# Exibir recomendação mesmo quando decisão for "sem entrada"?
SHOW_NOENTRY_RECOMMENDATION = False  # deixe True se quiser ver a rec. nas noentry

MIN_SPINS = 800         # amostra mínima para testar viés (estilo livros)
P_THRESHOLD = 0.01      # nível de significância aproximado (qui-quadrado, gl=36)

# Ordem física (roda europeia, sentido horário)
WHEEL_ORDER = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]

# -----------------------------------------------------------------------------
# ESTADO (por chat)
# -----------------------------------------------------------------------------
def make_default_state() -> Dict[str, Any]:
    return {
        "history": [],        # sequência de números informados
        "wins": 0,
        "losses": 0,
        "events": [],         # log por giro: {number, dz, outcome, d1, d2, excl, reason, gale_*}
        "counts": {i: 0 for i in range(37)},  # contagem por número
        "total_spins": 0,     # total de giros acumulados (para viés)

        # --- Estado do Gale controlado (máx. 1 passo) ---
        "gale_active": False,   # True => próxima rodada é Gale 1
        "gale_step": 0,         # 0 (desativado) | 1 (execução do Gale 1 nesta rodada)
        "gale_d1": None,
        "gale_d2": None,
        "gale_excl": None,
    }

STATE: Dict[int, Dict[str, Any]] = {}
def get_state(chat_id: int) -> Dict[str, Any]:
    if chat_id not in STATE:
        STATE[chat_id] = make_default_state()
    return STATE[chat_id]

# -----------------------------------------------------------------------------
# FUNÇÕES BÁSICAS
# -----------------------------------------------------------------------------
def dozen_of(n: int) -> str:
    if n == 0:
        return "Z"
    if 1 <= n <= 12:
        return "D1"
    if 13 <= n <= 24:
        return "D2"
    if 25 <= n <= 36:
        return "D3"
    return "?"

def bet_header(d1: str, d2: str, excl: str) -> str:
    return (
        f"🎯 <b>Recomendação</b>: {esc(d1)} + {esc(d2)}  |  🚫 <b>Excluída</b>: {esc(excl)}\n"
        f"📚 Estratégia: viés de roda + setor contíguo (estilo livros) | Gale: máx. 1 passo"
    )

def status_text(s: Dict[str, Any]) -> str:
    total_entries = s["wins"] + s["losses"]
    total_spins = len(s["history"])
    hit = (s["wins"] / total_entries * 100) if total_entries > 0 else 0.0
    gale_flag = f"{'Sim (próx.=Gale 1)' if s['gale_active'] and s['gale_step']==0 else ('Executando Gale 1' if s['gale_step']==1 else 'Não')}"
    return (
        "📊 <b>Status</b>\n"
        f"• Entradas: {total_entries} (✅ {s['wins']} / ❌ {s['losses']})  |  Taxa de acerto: {hit:.1f}%\n"
        f"• Giros lidos: {total_spins}  |  Sem entrada: {total_spins - total_entries}\n"
        f"• Amostra p/viés: {s['total_spins']} números acumulados\n"
        f"• Gale: {gale_flag}\n"
        "• Janela de setor: 8–12 pockets contíguos (ordem física da roda)"
    )

# -----------------------------------------------------------------------------
# VIÉS + MAPEAMENTO PARA DUAS DÚZIAS
# -----------------------------------------------------------------------------
def update_counts(s: Dict[str, Any], n: int) -> None:
    if 0 <= n <= 36:
        s["counts"][n] += 1
        s["total_spins"] += 1

def chi_square_bias(counts: Dict[int,int], total: int) -> Tuple[float, float]:
    """Qui-quadrado simples vs. uniforme (1/37). Retorna (chi2, p_approx)."""
    if total == 0:
        return (0.0, 1.0)
    expected = total / 37.0
    chi2 = 0.0
    for i in range(37):
        o = counts.get(i, 0)
        diff = o - expected
        chi2 += (diff * diff) / expected
    # p-value approx (sem SciPy), gl=36
    import math
    lam = 0.5 * chi2
    ssum, fact, powt = 0.0, 1.0, 1.0
    for k in range(19):
        if k > 0:
            fact *= k
            powt *= lam
        ssum += powt / fact
    p_approx = math.exp(-lam) * ssum
    return (chi2, min(max(p_approx, 0.0), 1.0))

def find_hottest_sector(counts: Dict[int,int], window_len: int = 12) -> List[int]:
    """Varre janelas contíguas na ordem da roda e retorna o setor com maior excesso sobre o esperado."""
    total = sum(counts.values())
    if total == 0:
        return []
    exp_per_num = total / 37.0
    best_sector: List[int] = []
    best_excess = float("-inf")
    n = len(WHEEL_ORDER)
    for L in range(8, window_len+1):  # sectores de 8 a 12 pockets
        for start in range(n):
            sector = [WHEEL_ORDER[(start + k) % n] for k in range(L)]
            obs = sum(counts[i] for i in sector)
            exp = exp_per_num * L
            excess = obs - exp
            if excess > best_excess:
                best_excess = excess
                best_sector = sector
    return best_sector

def sector_to_two_dozens(sector: List[int]) -> Tuple[str,str,str]:
    """Escolhe as 2 dúzias que mais cobrem o setor quente."""
    if not sector:
        return ("D1","D2","D3")
    cover = {"D1":0,"D2":0,"D3":0}
    for x in sector:
        dz = dozen_of(x)
        if dz in cover:
            cover[dz] += 1
    ordered = sorted(cover.items(), key=lambda kv:(-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = {"D1","D2","D3"}.difference({d1,d2}).pop()
    return (d1,d2,excl)

def should_enter_book_style(s: Dict[str, Any]) -> Tuple[bool, str, Tuple[str,str,str]]:
    """
    Estilo livros:
      - precisa de amostra grande (MIN_SPINS)
      - testa viés global (qui² vs uniforme)
      - encontra setor contíguo mais quente
      - mapeia setor -> duas dúzias
    """
    total = s.get("total_spins", 0)
    if total < MIN_SPINS:
        return (False, f"amostra insuficiente ({total}/{MIN_SPINS})", ("D1","D2","D3"))
    chi2, p = chi_square_bias(s["counts"], total)
    if p > P_THRESHOLD:
        return (False, f"sem viés detectável (p≈{p:.3f})", ("D1","D2","D3"))
    sector = find_hottest_sector(s["counts"], window_len=12)
    d1, d2, excl = sector_to_two_dozens(sector)
    return (True, "viés detectado", (d1,d2,excl))

# -----------------------------------------------------------------------------
# GALE HELPERS
# -----------------------------------------------------------------------------
def snapshot_gale(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "gale_active": s["gale_active"],
        "gale_step": s["gale_step"],
        "gale_d1": s["gale_d1"],
        "gale_d2": s["gale_d2"],
        "gale_excl": s["gale_excl"],
    }

def restore_gale(s: Dict[str, Any], snap: Dict[str, Any]) -> None:
    s["gale_active"] = snap.get("gale_active", False)
    s["gale_step"] = snap.get("gale_step", 0)
    s["gale_d1"] = snap.get("gale_d1")
    s["gale_d2"] = snap.get("gale_d2")
