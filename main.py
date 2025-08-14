# --- SUBSTITUA o on_startup atual por este ---
async def on_startup() -> None:
    global ptb_app
    if ptb_app is None:
        ptb_app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        # Defaults: modo CONSERVADOR
        ptb_app.bot_data["MODE"] = "conservador"
        ptb_app.bot_data["MIN_SPINS"] = 25
        ptb_app.bot_data["P_THRESHOLD"] = 0.05
        ptb_app.bot_data["WINDOW"] = 200
        ptb_app.bot_data["K"] = 14
        ptb_app.bot_data["NEED"] = 9

        # Handlers
        ptb_app.add_handler(CommandHandler("start", start_cmd))
        ptb_app.add_handler(CommandHandler("help", help_cmd))
        ptb_app.add_handler(CommandHandler("modo", modo_cmd))
        ptb_app.add_handler(CommandHandler("status", status_cmd))
        ptb_app.add_handler(CallbackQueryHandler(cb_handler))
        ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), number_message))

        # ORDEM CORRETA: initialize -> set_webhook -> start
        await ptb_app.initialize()
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        await ptb_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES
        )
        await ptb_app.start()
        log.info("PTB inicializado e webhook configurado em %s", webhook_url)

# --- SUBSTITUA o shutdown por este (a ordem já estava ok, mantive explícita) ---
@app.on_event("shutdown")
async def _shutdown():
    global ptb_app
    if ptb_app:
        try:
            await ptb_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await ptb_app.stop()
        await ptb_app.shutdown()
        log.info("PTB finalizado.")
