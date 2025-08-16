from __future__ import annotations

from typing import Dict

from .state import UserState


RESP_WAIT = ( "⏳ <i>Aguardar mais dados...</i>\n\n"
    "✏️ Se precisar corrigir o número digitado, use o comando <b>/corrigir</b>.")
RESP_ZERO = "\u2139\ufe0f Zero detectado, leitura reiniciada."
RESP_CORRECT = "\u2705 Último número corrigido para {num}. Análise atualizada:"


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
        f"\ud83c\udfaf Recomendação: {rec} | \ud83d\udeab Excluída: {excl}",
        f"\ud83d\udcd6 Justificativa: {reason}",
        f"\ud83d\udcca Histórico (últimos 12): {hist} | \ud83d\udd01 Pendentes: {pending}",
        f"\ud83d\udcb0 Sinalização de stake: {stake_msg}",
    ]
    return "\n".join(blocks)
