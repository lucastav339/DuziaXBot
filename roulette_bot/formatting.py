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
    if analysis.get("status") == "wait":
        return RESP_WAIT

    rec = analysis.get("recommendation", "")
    excl = analysis.get("excluded", "")
    reason = analysis.get("reason", "")
    hist = analysis.get("history", "")
    pending = analysis.get("pending", "0")
    stake_msg = (
        f"R$ {state.stake_value:.2f}" if state.stake_on else "sem stake definida"
    )

    blocks = [
        f"✅ Recomendação: {rec} \n\ud83d\udeab Excluída: {excl}",
        f"\ud83d\udcd6 Justificativa: {reason}",
        f"\ud83d\udcca Histórico (últimos 12):\n📋{hist}📋\n"
         "✏️ Para limpar o histórico::\n"
         "⚠️ Use o comando /reset."
         "📝 Para corrigir o número digitado:\n"
         "⚠️ Use o comando /corrigir."
        ,
    ]
    return "\n".join(blocks)
