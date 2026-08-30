import asyncio
import os
import random
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

TOKEN = __import__("re").search(r"\d{6,12}:[A-Za-z0-9_-]{35}", "".join(os.environ["BOT_TOKEN"].split())).group(0)
DB_PATH = os.getenv("DB_PATH", "casino.sqlite3")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
SPIN_COST = 25
DAILY_BONUS = 500

DEFAULT_PRIZES = [
    ("Мимо", 0, 5800),
    ("Возврат ставки", 25, 2000),
    ("Небольшой выигрыш", 50, 1400),
    ("Хороший выигрыш", 100, 600),
    ("Крупный выигрыш", 250, 180),
    ("ДЖЕКПОТ", 1000, 20),
]

router = Router()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            points INTEGER NOT NULL DEFAULT 0,
            last_daily TEXT
        );
        CREATE TABLE IF NOT EXISTS prizes (
            prize_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            payout INTEGER NOT NULL,
            weight INTEGER NOT NULL
        );
        """)
        count = (await (await db.execute("SELECT COUNT(*) FROM prizes")).fetchone())[0]
        if count == 0:
            await db.executemany(
                "INSERT INTO prizes(name, payout, weight) VALUES (?, ?, ?)", DEFAULT_PRIZES
            )
        await db.commit()


async def ensure_user(message: Message, db: aiosqlite.Connection):
    username = message.from_user.username if message.from_user else None
    await db.execute(
        "INSERT INTO users(user_id, username) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
        (message.from_user.id, username),
    )


@router.message(CommandStart())
async def start(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(message, db)
        await db.commit()
    await message.answer(
        "🎰 Добро пожаловать! Здесь играют только на виртуальные очки.\n\n"
        "Каждый день: /daily — 500 очков\n"
        "Один слот: /spin — 25 очков\n"
        "Баланс: /balance\n"
        "Таблица выигрышей: /prizes\n\n"
        "Очки нельзя купить, продать или вывести."
    )


@router.message(Command("balance"))
async def balance(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(message, db)
        row = await (await db.execute(
            "SELECT points FROM users WHERE user_id=?", (message.from_user.id,)
        )).fetchone()
        await db.commit()
    await message.answer(f"💰 Баланс: {row[0]} очков")


@router.message(Command("daily"))
async def daily(message: Message):
    today = datetime.now(timezone.utc).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(message, db)
        cursor = await db.execute(
            "UPDATE users SET points=points+?, last_daily=? "
            "WHERE user_id=? AND (last_daily IS NULL OR last_daily<>?)",
            (DAILY_BONUS, today, message.from_user.id, today),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT points FROM users WHERE user_id=?", (message.from_user.id,)
        )).fetchone()
    if cursor.rowcount:
        await message.answer(f"🎁 Получено {DAILY_BONUS} очков! Баланс: {row[0]}")
    else:
        await message.answer("Сегодня бонус уже получен. Возвращайся завтра после 00:00 UTC.")


@router.message(Command("spin"))
@router.message(F.dice.emoji == "🎰")
async def spin(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await ensure_user(message, db)
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "UPDATE users SET points=points-? WHERE user_id=? AND points>=?",
            (SPIN_COST, message.from_user.id, SPIN_COST),
        )
        if not cursor.rowcount:
            await db.rollback()
            await message.answer("Не хватает очков. Забери ежедневные: /daily")
            return
        prizes = await (await db.execute(
            "SELECT name, payout, weight FROM prizes ORDER BY prize_id"
        )).fetchall()
        if sum(p[2] for p in prizes) != 10_000:
            await db.rollback()
            await message.answer("Таблица вероятностей временно настроена неверно.")
            return
        name, payout, _ = random.choices(prizes, weights=[p[2] for p in prizes], k=1)[0]
        await db.execute(
            "UPDATE users SET points=points+? WHERE user_id=?", (payout, message.from_user.id)
        )
        points = (await (await db.execute(
            "SELECT points FROM users WHERE user_id=?", (message.from_user.id,)
        )).fetchone())[0]
        await db.commit()

    # Telegram controls the visual dice value; the prize is calculated above.
    await message.answer_dice("🎰")
    if payout:
        await message.answer(f"✨ {name}: +{payout} очков\nБаланс: {points}")
    else:
        await message.answer(f"Пусто. Повезёт в следующий раз!\nБаланс: {points}")


@router.message(Command("prizes"))
async def prizes(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT name, payout, weight FROM prizes ORDER BY payout"
        )).fetchall()
    lines = ["🎰 Выигрыши и вероятность:"]
    lines += [f"{name}: {payout} очков — {weight / 100:.2f}%" for name, payout, weight in rows]
    lines.append("\nСредняя отдача: 98%. Анимация слота декоративная.")
    await message.answer("\n".join(lines))


@router.message(Command("admin_probs"))
async def admin_probs(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT prize_id, name, payout, weight FROM prizes ORDER BY prize_id"
        )).fetchall()
    text = ["Формат: /setprobs 1=58 2=20 3=14 4=6 5=1.8 6=0.2"]
    text += [f"{pid}. {name}, {payout}: {weight / 100:.2f}%" for pid, name, payout, weight in rows]
    await message.answer("\n".join(text))


@router.message(Command("setprobs"))
async def set_probabilities(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    parts = (message.text or "").split()[1:]
    if not parts:
        await message.answer("Пример: /setprobs 1=58 2=20 3=14 4=6 5=1.8 6=0.2")
        return
    try:
        updates = {}
        for part in parts:
            raw_id, raw_percent = part.split("=", 1)
            updates[int(raw_id)] = round(float(raw_percent.replace(",", ".")) * 100)
        if any(not 0 <= weight <= 10_000 for weight in updates.values()):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Неверный формат. Пример: /setprobs 1=58 2=20 3=14 4=6 5=1.8 6=0.2")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT prize_id, weight FROM prizes")).fetchall()
        current = dict(rows)
        if not updates.keys() <= current.keys():
            await message.answer("Один из указанных ID не существует.")
            return
        current.update(updates)
        if sum(current.values()) != 10_000:
            await message.answer(
                f"Не сохранено: сумма станет {sum(current.values()) / 100:.2f}%, нужна ровно 100%."
            )
            return
        await db.executemany(
            "UPDATE prizes SET weight=? WHERE prize_id=?",
            [(weight, prize_id) for prize_id, weight in updates.items()],
        )
        await db.commit()
    await message.answer("Вероятности сохранены.")


async def main():
    await init_db()
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
