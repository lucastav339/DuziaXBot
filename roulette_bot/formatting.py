from __future__ import annotations

from typing import Dict
from .state import UserState

RESP_WAIT = (
    "⏳ Aguardando mais dados para análise.\n"
    "🎲 Envie o próximo número (0–36).\n"
    "✏️ Para corrigir o número digitado:\n"
    "✨ Use o comando /corrigir."
)
RESP_ZERO = "\u2139\ufe0f Zero detectado, leitura reiniciada."
RESP_CORRECT = "\u2705 Último número corrigido para {num}.\n⚡Análise atualizada:"


def format_response(state: UserState, analysis: Dict[str, str]) -> str:
    # Bandeira de gale (para exibir status)
    gale_block = ""
    if state.gale_enabled:
        if state.gale_left > 0 and state.gale_dozen:
            gale_block = f"🌀 Gale: ATIVO (1/1) na {state.gale_dozen}\n"
        else:
            gale_block = "🌀 Gale: pronto (1/1)\n"

    if analysis.get("status") == "wait":
        return gale_block + RESP_WAIT

    rec = analysis.get("recommendation", "")
    excl = analysis.get("excluded", "")
    reason = analysis.get("reason", "")
    hist = analysis.get("history", "")
    # pending = analysis.get("pending", "0")  # não usado visualmente

    perf_block = ""
    if state.current_rec:
        plays = state.rec_plays
        hits = state.rec_hits
        misses = state.rec_misses
        acc = f"{(hits / plays * 100):.1f}%" if plays > 0 else "—"
        perf_block = (
            "📊 Desempenho (cumulativo):\n"
            f"• Jogadas: {plays} | ✅ Acertos: {hits} | ❌ Erros: {misses} | 🎯 Taxa: {acc}\n"
        )

    blocks = [
        gale_block.rstrip(),
        f"✅ Recomendação (single): 🌟{rec}🌟 \n\ud83d\udeab Excluída: {excl}",
        f"\ud83d\udcd6 Justificativa: {reason}",
        perf_block.rstrip(),
        (
            f"\ud83d\udcca Histórico (últimos 12):\n📋{hist}📋\n"
            "✏️ Para limpar o histórico: /reset\n"
            "📝 Para corrigir o número: /corrigir <número>"
        ),
    ]
    blocks = [b for b in blocks if b]
    return "\n".join(blocks)
