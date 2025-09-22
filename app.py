import asyncio
import os
import logging
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Defina a variável de ambiente TELEGRAM_BOT_TOKEN.")

bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# ... (seus handlers aqui)

async def main():
    # 🔑 Garante que não há webhook ativo (modo webhook conflita com polling)
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.warning(f"Não foi possível deletar webhook: {e}")

    # Inicia polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
