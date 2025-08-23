import os
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from telegram import Update
from telegram.ext import Application

log = logging.getLogger("runtime")

def get_mode_and_url():
    mode = "webhook" if os.getenv("USE_WEBHOOK", "true").lower() in ("1", "true") else "polling"
    public_url = os.getenv("WEBHOOK_URL", "").strip()
    if mode == "webhook" and not public_url:
        raise RuntimeError("USE_WEBHOOK=true mas WEBHOOK_URL não foi definido.")
    return mode, public_url

def build_webhook_app(application: Application, public_url: str) -> FastAPI:
    app = FastAPI()

    @app.on_event("startup")
    async def on_startup():
        url = public_url.rstrip("/") + "/webhook"
        log.info(f"[startup] Configurando webhook em: {url}")
        await application.initialize()
        await application.start()
        ok = await application.bot.set_webhook(url=url, drop_pending_updates=True)
        info = await application.bot.get_webhook_info()
        log.info(f"[startup] set_webhook ok={ok} info={info.to_dict()}")

    @app.on_event("shutdown")
    async def on_shutdown():
        log.info("[shutdown] Removendo webhook e parando PTB...")
        await application.bot.delete_webhook()
        await application.stop()
        await application.shutdown()
        log.info("[shutdown] Encerrado.")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/debug/webhook-info")
    async def debug_webhook_info():
        info = await application.bot.get_webhook_info()
        return info.to_dict()

    @app.post("/webhook")
    async def telegram_webhook(request: Request):
        data = await request.json()
        log.info(f"[webhook] Update recebido: keys={list(data.keys())}")
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse({"ok": True})

    return app
