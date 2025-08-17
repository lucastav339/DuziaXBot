# bot.py (trechos relevantes)

from roulette_bot.state import UserState
from roulette_bot.analysis import analyze, validate_number, number_to_dozen
from roulette_bot.formatting import format_response, RESP_ZERO, RESP_CORRECT
# ... demais imports e setup inalterados ...

async def handle_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    ok, num = validate_number(text)
    if not ok or num is None:
        await safe_reply(update.message, "Entrada inválida. Envie apenas números de 0 a 36.")
        return

    state = get_state(update.effective_chat.id)

    # 1) Computa resultado da recomendação ANTERIOR SOMENTE se havia aposta aberta
    if state.has_open_rec and state.current_rec and num != 0:
        dz = number_to_dozen(num)
        state.rec_plays += 1
        if dz in state.current_rec:
            state.rec_hits += 1
            state.loss_streak = 0
            # NOVO: ao acertar, inicia o ciclo pós-acerto (coletar 5, analisar, zerar, repetir)
            state.start_post_win_cycle(collect_n=5)
        else:
            state.rec_misses += 1
            state.loss_streak += 1
            if state.loss_streak >= state.max_loss_streak and state.cooldown_left == 0 and state.conservative_boost:
                state.cooldown_left = state.cooldown_spins
                state.loss_streak = 0
        # fecha a aposta aberta para não contar de novo em espera
        state.has_open_rec = False
        state.current_rec = None

    # 2) Zero: limpa somente o histórico (placar cumulativo preservado)
    if num == 0:
        state.reset_history()
        await safe_reply(update.message, RESP_ZERO)
        return

    # 3) Adiciona número ao histórico + incrementa contador de giros
    state.add_number(num)
    state.spin_count += 1

    # 3.1) Se estivermos no ciclo pós-acerto COLETANDO, apenas aguardar
    if state.step_post_win_collect():
        await safe_reply(update.message, format_response(state, {"status": "wait"}))
        return

    # 4) Se estiver em cooldown (apenas quando boost ativo), decrementar e responder WAIT
    if state.cooldown_left > 0:
        state.cooldown_left -= 1
        await safe_reply(update.message, format_response(state, {"status": "wait"}))
        return

    # 4.1) Throttle de ritmo (pulado se ciclo pós-acerto acabou de liberar análise)
    if state.spin_count - state.last_entry_spin < state.min_spins_between_entries:
        await safe_reply(update.message, format_response(state, {"status": "wait"}))
        return

    # 5) Checa taxa e dispara modo conservador se necessário (lógica existente)
    if (state.rec_plays >= state.min_samples_for_eval
        and (state.rec_hits / max(1, state.rec_plays)) <= state.acc_trigger
        and not state.conservative_boost):
        state.conservative_boost = True
        await safe_reply(
            update.message,
            "🛡️ Entrando em <b>modo conservador</b> para equilibrar a taxa de acerto.\n"
            "🔧 Critérios mais rígidos temporariamente aplicados.",
            parse_mode="HTML"
        )

    # 6) Roda análise (adaptativa: normal vs conservadora)
    analysis = analyze(state)

    # 7) Atualiza recomendação ativa e ABRE aposta somente se houver recomendação
    if analysis.get("status") == "ok":
        rec_text = analysis.get("recommendation", "")  # ex. "D2 + D3" (invertida)
        new_set = set(x.strip() for x in rec_text.split("+") if x.strip())
        state.set_recommendation(new_set)
        state.last_entry_spin = state.spin_count  # marca última entrada
        state.has_open_rec = True                # abre aposta
    else:
        # sem recomendação: garante que não há aposta aberta
        state.has_open_rec = False
        state.current_rec = None

    # 8) Responde a análise ao usuário
    msg = format_response(state, analysis)
    await safe_reply(update.message, msg)

    # 9) NOVO: se o ciclo pós-acerto está ativo e acabamos de ANALISAR,
    # zera histórico e reinicia a coleta de 5 giros para manter "sempre assim".
    if state.post_win_mode_active and state.post_win_wait_left == 0:
        state.reset_history()
        state.restart_post_win_collect(collect_n=5)
