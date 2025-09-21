import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import CommandStart, Command

from scraper import fetch_latest_result

logging.basicConfig(level=logging.INFO)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Defina a variável de ambiente TELEGRAM_BOT_TOKEN.")

bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

def color_of(n: int) -> str:
    if n == 0:
        return "🟢 Verde"
    red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    return "🔴 Vermelho" if n in red else "⚫️ Preto"

def parity_of(n: int) -> str:
    return "Neutro" if n == 0 else ("Par" if n % 2 == 0 else "Ímpar")

def fmt_number(n: int) -> str:
    return f"<b>{n}</b> • {color_of(n)} • {parity_of(n)}"

async def send_latest(message: Message):
    await message.answer("⏳ Buscando o último número da Evolution Speed Roulette…")
    try:
        n = await fetch_latest_result()
        if n is None:
            await message.answer("❌ Não consegui capturar o número agora. Tente novamente em alguns segundos.")
        else:
            await message.answer(f"🎯 Último número: {fmt_number(n)}")
    except Exception as e:
        await message.answer(f"❌ Erro ao buscar: <code>{e}</code>")

@dp.message(CommandStart())
async def on_start(message: Message):
    await send_latest(message)

@dp.message(F.text.casefold() == "agora")
@dp.message(Command(commands=["agora"]))
async def on_agora(message: Message):
    await send_latest(message)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
