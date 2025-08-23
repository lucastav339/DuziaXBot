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
        await application.initialize()
        await application.start()
        await application.bot.set_webhook(url=url, drop_pending_updates=True)

    @app.on_event("shutdown")
    async def on_shutdown():
        await application.bot.delete_webhook()
        await application.stop()
        await application.shutdown()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/webhook")
    async def telegram_webhook(request: Request):
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return JSONResponse({"ok": True})

    return app
