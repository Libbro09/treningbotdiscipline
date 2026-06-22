# -*- coding: utf-8 -*-
"""
Бот-тренер по дисциплине и привычкам.

Стек:
- aiogram 3.x        — Telegram
- APScheduler        — напоминания (утро / вечер)
- SQLite (sqlite3)   — встроенная БД
- DeepSeek API       — умный тренер (модель deepseek-chat)

Запуск:  python bot.py
Конфиг:  через .env (см. .env.example)
"""

import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

import google.generativeai as genai
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

try:
    from zoneinfo import ZoneInfo
except ImportError:  # очень старый Python
    ZoneInfo = None

# --------------------------------------------------------------------------- #
#                                  КОНФИГ                                      #
# --------------------------------------------------------------------------- #
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
TZ_NAME = os.getenv("TZ", "Europe/Moscow")
DB_PATH = os.getenv("DB_PATH", "habits.db")
PORT = int(os.getenv("PORT", "8080"))

TZ = ZoneInfo(TZ_NAME) if ZoneInfo else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("discipline-bot")

# Привычки: ключ -> (название для плана, короткая надпись на кнопке)
HABITS = {
    "water": ("Пить воду 💧", "Сделал(а) воду"),
    "exercise": ("Зарядка (10–15 мин) 🤸", "Сделал(а) зарядку"),
    "cold_shower": ("Холодный душ 🚿", "Сделал(а) холодный душ"),
}
TOTAL_HABITS = len(HABITS)
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

WELCOME = (
    "Привет! Я твой тренер по дисциплине. Каждый день я буду помогать тебе "
    "вырабатывать привычки.\n\nНачнём с основ: подъём в 7:00, вода, зарядка, "
    "холодный душ. 💪"
)

bot: Bot = None  # назначается в main()
router = Router()
scheduler = AsyncIOScheduler(timezone=TZ_NAME)


# --------------------------------------------------------------------------- #
#                                  ВРЕМЯ                                       #
# --------------------------------------------------------------------------- #
def now_local() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def parse_time(s: str):
    """'7:00' / '07.00' / '7 0' -> '07:00' либо None."""
    s = (s or "").strip()
    for fmt in ("%H:%M", "%H.%M", "%H %M"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
#                                    БД                                        #
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                name         TEXT,
                wake_time    TEXT DEFAULT '07:00',
                report_time  TEXT DEFAULT '00:00',
                check_time   TEXT DEFAULT '14:00',
                goals        TEXT,
                last_morning TEXT,
                last_report  TEXT,
                last_check   TEXT,
                created_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS habits (
                user_id INTEGER,
                date    TEXT,
                habit   TEXT,
                status  INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date, habit)
            );
            CREATE TABLE IF NOT EXISTS progress (
                user_id    INTEGER,
                date       TEXT,
                done_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            );
            """
        )
    migrate_db()
    log.info("БД готова: %s", DB_PATH)


def migrate_db():
    """Добавляет новые колонки в уже существующие БД (без потери данных)."""
    new_cols = {
        "check_time": "TEXT DEFAULT '14:00'",
        "goals": "TEXT",
        "last_check": "TEXT",
    }
    with db() as conn:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, decl in new_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
                log.info("Миграция: добавлена колонка users.%s", col)


def register_user(user_id: int, name: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, name, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name = excluded.name",
            (user_id, name, now_local().isoformat()),
        )


def get_user(user_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()


def set_user_time(user_id: int, field: str, value: str):
    assert field in ("wake_time", "report_time", "check_time")
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


def set_user_goals(user_id: int, goals: str):
    with db() as conn:
        conn.execute("UPDATE users SET goals = ? WHERE user_id = ?", (goals, user_id))


def mark_habit(user_id: int, habit: str, day: str = None) -> int:
    """Отмечает привычку выполненной, пересчитывает progress, возвращает кол-во."""
    day = day or today_str()
    with db() as conn:
        conn.execute(
            "INSERT INTO habits (user_id, date, habit, status) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(user_id, date, habit) DO UPDATE SET status = 1",
            (user_id, day, habit),
        )
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM habits "
            "WHERE user_id = ? AND date = ? AND status = 1",
            (user_id, day),
        ).fetchone()["c"]
        conn.execute(
            "INSERT INTO progress (user_id, date, done_count) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, date) DO UPDATE SET done_count = excluded.done_count",
            (user_id, day, count),
        )
    return count


def get_done_habits(user_id: int, day: str = None) -> set:
    day = day or today_str()
    with db() as conn:
        rows = conn.execute(
            "SELECT habit FROM habits WHERE user_id = ? AND date = ? AND status = 1",
            (user_id, day),
        ).fetchall()
    return {r["habit"] for r in rows}


def get_streak(user_id: int) -> int:
    """Серия подряд идущих дней, где выполнены ВСЕ привычки."""
    with db() as conn:
        rows = conn.execute(
            "SELECT date, done_count FROM progress WHERE user_id = ?", (user_id,)
        ).fetchall()
    done_by_date = {r["date"]: r["done_count"] for r in rows}

    cur = now_local().date()
    # Сегодняшний день ещё не закончился — не рвём серию, если он не закрыт.
    if done_by_date.get(cur.isoformat(), 0) < TOTAL_HABITS:
        cur = cur - timedelta(days=1)

    streak = 0
    while done_by_date.get(cur.isoformat(), 0) >= TOTAL_HABITS:
        streak += 1
        cur = cur - timedelta(days=1)
    return streak


def get_week_stats(user_id: int):
    """Возвращает (список дат за 7 дней, словарь дата->кол-во, процент)."""
    today = now_local().date()
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    with db() as conn:
        rows = conn.execute(
            "SELECT date, done_count FROM progress WHERE user_id = ? AND date >= ?",
            (user_id, days[0].isoformat()),
        ).fetchall()
    done_by_date = {r["date"]: r["done_count"] for r in rows}
    possible = len(days) * TOTAL_HABITS
    done = sum(done_by_date.get(d.isoformat(), 0) for d in days)
    percent = round(done / possible * 100) if possible else 0
    return days, done_by_date, percent


def build_graph(days, done_by_date) -> str:
    lines = []
    for d in days:
        c = done_by_date.get(d.isoformat(), 0)
        bars = "█" * c + "░" * (TOTAL_HABITS - c)
        lines.append(f"{WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}  {bars}  {c}/{TOTAL_HABITS}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
#                              КЛАВИАТУРЫ / ТЕКСТЫ                             #
# --------------------------------------------------------------------------- #
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    done = get_done_habits(user_id)
    rows = []
    for key, (_, short) in HABITS.items():
        mark = "✅" if key in done else "⬜"
        rows.append([InlineKeyboardButton(text=f"{mark} {short}", callback_data=f"done:{key}")])
    rows.append(
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
            InlineKeyboardButton(text="🗓 Мой график", callback_data="schedule"),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(user) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⏰ Подъём: {user['wake_time']}", callback_data="set_wake")],
            [InlineKeyboardButton(text=f"☀️ Чек-ин днём: {user['check_time']}", callback_data="set_check")],
            [InlineKeyboardButton(text=f"🌙 Отчёт: {user['report_time']}", callback_data="set_report")],
            [InlineKeyboardButton(text="🎯 Мои цели", callback_data="set_goals")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ]
    )


def plan_text(name: str = None) -> str:
    hi = f"Доброе утро, {name}!" if name else "Доброе утро!"
    return (
        f"{hi} Пора просыпаться и выполнять привычки.\n\n"
        "📋 <b>План на сегодня:</b>\n"
        "💧 Пить воду\n"
        "🤸 Зарядка (10–15 минут)\n"
        "🚿 Холодный душ\n\n"
        "Отмечай выполненное кнопками ниже 👇"
    )


# --------------------------------------------------------------------------- #
#                            УМНЫЙ ТРЕНЕР (Gemini)                             #
# --------------------------------------------------------------------------- #
COACH_SYSTEM = (
    "Ты — личный тренер по дисциплине, привычкам и здоровому образу жизни. "
    "Общаешься как живой человек, а не робот: тепло, по-человечески, с эмоциями, на «ты». "
    "Ты искренне интересуешься, как у человека дела и как проходит день. "
    "Если человек молодец — искренне хвали. Если ленится — по-доброму, но честно встряхни, "
    "без оскорблений и без давления. "
    "Очень важно: ты заботишься о здоровье человека и НЕ предлагаешь ничего экстремального "
    "или вредного. Высыпаться (7–8 часов), пить воду, разумные нагрузки, отдых и баланс — "
    "это всегда приоритет. Никакого фанатизма и выгорания. "
    "Давай конкретные, выполнимые советы. Иногда задавай человеку короткий встречный вопрос про его день, "
    "самочувствие или настроение, чтобы поддержать диалог. "
    "Отвечай на русском, 2–5 предложений, живым языком, без markdown и без звёздочек."
)

# Для построения распорядка дня — здесь списки и структура УМЕСТНЫ.
SCHEDULE_SYSTEM = (
    "Ты — опытный тренер по дисциплине и режиму дня, который заботится о здоровье человека. "
    "Составь реалистичный, выполнимый и здоровый распорядок дня по часам. "
    "Обязательно учти: полноценный сон 7–8 часов, питьё воды в течение дня, зарядка 10–15 минут, "
    "холодный душ, приёмы пищи, перерывы и отдых. Режим НЕ должен быть изматывающим — без фанатизма, "
    "с запасом на отдых и восстановление, чтобы человек не выгорел. "
    "Пиши на русском, по времени (например '07:00 — подъём, стакан воды'), коротко и понятно. "
    "В конце добавь 2–3 тёплых совета, как держаться режима и не сорваться. Без markdown и без звёздочек."
)


def user_context(user_id: int) -> str:
    done = get_done_habits(user_id)
    streak = get_streak(user_id)
    _, _, percent = get_week_stats(user_id)
    u = get_user(user_id)
    done_l = [HABITS[k][0] for k in HABITS if k in done]
    miss_l = [HABITS[k][0] for k in HABITS if k not in done]
    ctx = (
        f"Имя: {u['name'] if u else 'друг'}. "
        f"Сегодня выполнено: {', '.join(done_l) or 'ничего'}. "
        f"Пропущено сегодня: {', '.join(miss_l) or 'ничего'}. "
        f"Серия: {streak} дней подряд. Выполнение за неделю: {percent}%."
    )
    if u and u["goals"]:
        ctx += f" Цели человека: {u['goals']}."
    return ctx


async def gemini_generate(prompt: str, max_tokens: int = 400) -> str:
    """Низкоуровневый вызов Gemini в отдельном потоке (не блокирует бота)."""
    if not GOOGLE_API_KEY:
        log.warning("Google API ключ не задан")
        return None
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash")
        log.info("→ Запрос к Gemini (%s токенов): %s", max_tokens, prompt[-60:])
        response = await asyncio.to_thread(
            model.generate_content,
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.9,
                max_output_tokens=max_tokens,
            ),
        )
        result = response.text.strip()
        log.info("← Gemini OK: %s", result[:50])
        return result
    except Exception as e:
        log.error("Ошибка Gemini: %s", e)
        return None


async def ask_coach(user_text: str, context_info: str = "") -> str:
    """Ответ тренера на сообщение пользователя."""
    prompt = COACH_SYSTEM + "\n\n"
    if context_info:
        prompt += "Данные пользователя: " + context_info + "\n\n"
    prompt += "Ответь живо, как настоящий тренер:\n\n" + user_text
    return await gemini_generate(prompt, 400)


async def build_schedule(user) -> str:
    """Строит персональный распорядок дня под цели человека."""
    goals = (user["goals"] or "").strip() or "выработать дисциплину, лучше высыпаться и быть в форме"
    prompt = (
        SCHEDULE_SYSTEM
        + f"\n\nПодъём человека: {user['wake_time']}. "
        + f"Вечерний отбой ориентируй так, чтобы было 7–8 часов сна. "
        + f"Цели человека: {goals}. "
        + "Составь распорядок на день."
    )
    return await gemini_generate(prompt, 1100)


# --------------------------------------------------------------------------- #
#                                  ОТЧЁТЫ                                      #
# --------------------------------------------------------------------------- #
async def send_stats(user_id: int):
    streak = get_streak(user_id)
    days, done_by_date, percent = get_week_stats(user_id)
    graph = build_graph(days, done_by_date)
    text = (
        "📊 <b>Твоя статистика</b>\n\n"
        f"🔥 Серия: <b>{streak}</b> дней подряд\n"
        f"📈 Выполнение за неделю: <b>{percent}%</b>\n\n"
        "<b>График за 7 дней:</b>\n"
        f"<pre>{graph}</pre>"
    )
    await bot.send_message(user_id, text)


async def send_ai(uid: int, text: str, **kwargs):
    """Отправка текста от ИИ без HTML-разметки (чтобы спецсимволы не ломали отправку)."""
    await bot.send_message(uid, text, parse_mode=None, **kwargs)


async def send_morning(user):
    uid = user["user_id"]
    await bot.send_message(uid, plan_text(user["name"]), reply_markup=main_menu_kb(uid))


async def send_report(user):
    uid = user["user_id"]
    await bot.send_message(uid, "🌙 Время отчёта о дне. Что ты сделал(а)?")
    await send_stats(uid)

    done = get_done_habits(uid)
    ctx = user_context(uid)
    if len(done) == TOTAL_HABITS:
        prompt = "Сегодня я выполнил все привычки. Подведи итог дня и подбодри меня перед сном."
        fallback = "💪 Отлично! Все привычки выполнены — ты молодец! Спи спокойно, завтра даём ещё."
    elif len(done) == 0:
        prompt = "Сегодня я не выполнил ни одной привычки. Честно оцени это и мотивируй не сдаваться."
        fallback = "Сегодня не получилось, но это не конец. Завтра у нас новый шанс 💪 Не сдавайся!"
    else:
        prompt = "Подведи итог моего дня по привычкам, отметь что я пропустил и дай совет на завтра."
        fallback = "Хороший прогресс! Завтра доделаем оставшиеся привычки. Спокойной ночи 🌙"

    analysis = await ask_coach(prompt, ctx)
    await send_ai(uid, analysis or fallback)


async def send_checkin(user):
    """Дневной чек-ин: бот по-дружески интересуется, как проходит день."""
    uid = user["user_id"]
    ctx = user_context(uid)
    done = get_done_habits(uid)
    if len(done) == TOTAL_HABITS:
        prompt = (
            "Сейчас середина дня. Я уже выполнил все привычки. По-дружески поинтересуйся, "
            "как проходит мой день и как самочувствие, и похвали. Задай короткий вопрос."
        )
        fallback = "Как проходит день? 🙂 Вижу, привычки уже закрыл — красавчик! Как настроение?"
    else:
        miss = [HABITS[k][0] for k in HABITS if k not in done]
        prompt = (
            "Сейчас середина дня. Поинтересуйся по-дружески, как проходит мой день и как я себя чувствую. "
            f"Мягко напомни, что ещё осталось сделать: {', '.join(miss)}. Задай короткий вопрос про день."
        )
        fallback = f"Привет! Как проходит день? 🙂 Не забудь сегодня: {', '.join(miss)}. Расскажи, как ты?"
    text = await ask_coach(prompt, ctx)
    await send_ai(uid, text or fallback, reply_markup=main_menu_kb(uid))


# --------------------------------------------------------------------------- #
#                                ПЛАНИРОВЩИК                                   #
# --------------------------------------------------------------------------- #
async def minute_tick():
    """Раз в минуту: рассылаем утренние планы и вечерние отчёты по времени юзера."""
    hhmm = now_local().strftime("%H:%M")
    today = today_str()
    with db() as conn:
        users = conn.execute("SELECT * FROM users").fetchall()

    for u in users:
        uid = u["user_id"]
        try:
            if u["wake_time"] == hhmm and u["last_morning"] != today:
                await send_morning(u)
                set_user_last(uid, "last_morning", today)
            if u["check_time"] == hhmm and u["last_check"] != today:
                await send_checkin(u)
                set_user_last(uid, "last_check", today)
            if u["report_time"] == hhmm and u["last_report"] != today:
                await send_report(u)
                set_user_last(uid, "last_report", today)
        except Exception:
            log.exception("Ошибка рассылки для user_id=%s", uid)


def set_user_last(user_id: int, field: str, value: str):
    assert field in ("last_morning", "last_report", "last_check")
    with db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))


# --------------------------------------------------------------------------- #
#                                   FSM                                        #
# --------------------------------------------------------------------------- #
class SettingsForm(StatesGroup):
    wake = State()
    report = State()
    check = State()
    goals = State()


# --------------------------------------------------------------------------- #
#                                ХЕНДЛЕРЫ                                      #
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message):
    register_user(message.from_user.id, message.from_user.first_name or "друг")
    await message.answer(WELCOME)

    if not GOOGLE_API_KEY:
        await message.answer(
            "⚠️ <i>Примечание:</i> Google API не задан. Бот будет отвечать заглушками. "
            "Чтобы включить умного тренера, добавь GOOGLE_API_KEY в .env и перезапусти.\n"
            "Проверь: /test_api"
        )
    else:
        await message.answer("🤖 Умный тренер (Google Gemini) включен! ✅")

    await message.answer(
        plan_text(message.from_user.first_name), reply_markup=main_menu_kb(message.from_user.id)
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    register_user(message.from_user.id, message.from_user.first_name or "друг")
    await message.answer(
        plan_text(message.from_user.first_name), reply_markup=main_menu_kb(message.from_user.id)
    )


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await send_stats(message.from_user.id)


async def send_schedule(uid: int, chat_message: Message):
    u = get_user(uid)
    if not u:
        register_user(uid, "друг")
        u = get_user(uid)
    if not u["goals"]:
        await chat_message.answer(
            "Чтобы построить график именно под тебя, расскажи о своих целях 🎯\n"
            "Например: «хочу высыпаться, привести себя в форму и меньше прокрастинировать».\n\n"
            "Напиши их командой /goals — и я составлю распорядок дня."
        )
        return
    await chat_message.answer("🗓 Строю твой персональный график дня... секунду")
    try:
        await bot.send_chat_action(uid, "typing")
    except Exception:
        pass
    schedule = await build_schedule(u)
    if schedule:
        await send_ai(uid, "🗓 Твой персональный график дня:\n\n" + schedule, reply_markup=main_menu_kb(uid))
    else:
        await chat_message.answer(
            "Не получилось построить график (ИИ не ответил). Проверь /test_api и попробуй ещё раз."
        )


@router.message(Command("plan"))
async def cmd_plan(message: Message):
    await send_schedule(message.from_user.id, message)


@router.message(Command("goals"))
async def cmd_goals(message: Message, state: FSMContext):
    register_user(message.from_user.id, message.from_user.first_name or "друг")
    await state.set_state(SettingsForm.goals)
    u = get_user(message.from_user.id)
    cur = f"\n\nСейчас твои цели: {u['goals']}" if u and u["goals"] else ""
    await message.answer(
        "Расскажи, чего ты хочешь добиться 🎯\n"
        "Например: «высыпаться, набрать форму, перестать прокрастинировать, меньше стресса».\n"
        "Напиши одним сообщением — и я учту это в графике и советах." + cur
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    u = get_user(message.from_user.id)
    if not u:
        register_user(message.from_user.id, message.from_user.first_name or "друг")
        u = get_user(message.from_user.id)
    await message.answer("⚙️ <b>Настройки</b>\nНажми, чтобы изменить время:", reply_markup=settings_kb(u))


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Я — твой тренер по дисциплине 💪\n\n"
        "/menu — план дня и кнопки\n"
        "/stats — статистика\n"
        "/plan — построить персональный график дня\n"
        "/goals — задать свои цели\n"
        "/settings — время подъёма, чек-ина и отчёта\n"
        "/test_api — проверить ИИ\n\n"
        "Я буду будить утром, интересоваться днём в обед и подводить итоги вечером. "
        "А ещё можешь просто написать мне — отвечу как живой тренер 💬"
    )


@router.message(Command("test_api"))
async def cmd_test_api(message: Message):
    if not GOOGLE_API_KEY:
        await message.answer("❌ Google API ключ не задан в .env")
        return

    await message.answer("🔍 Тестирую Google Gemini...")
    reply = await ask_coach("Скажи очень коротко (одно слово): привет!", "")

    if reply:
        await message.answer(f"✅ Gemini работает!\n\n{reply}")
    else:
        await message.answer(
            "❌ Gemini не отвечает. Проверь:\n"
            "1. Ключ в .env правильный?\n"
            "2. API включен в Google Cloud Console?\n"
            "3. Смотри логи бота (нажми Logs на Railway)\n\n"
            "Логи помогут найти конкретную ошибку 🔍"
        )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    await call.answer()
    await call.message.answer(plan_text(call.from_user.first_name), reply_markup=main_menu_kb(call.from_user.id))


@router.callback_query(F.data.startswith("done:"))
async def cb_done(call: CallbackQuery):
    habit = call.data.split(":", 1)[1]
    if habit not in HABITS:
        await call.answer()
        return

    already = habit in get_done_habits(call.from_user.id)
    count = mark_habit(call.from_user.id, habit)
    await call.answer("Уже отмечено" if already else "Отмечено! 💪")

    try:
        await call.message.edit_reply_markup(reply_markup=main_menu_kb(call.from_user.id))
    except Exception:
        pass

    if count == TOTAL_HABITS and not already:
        ctx = user_context(call.from_user.id)
        msg = await ask_coach(
            "Я выполнил все привычки на сегодня! Похвали меня и подбодри.", ctx
        )
        if not msg:
            streak = get_streak(call.from_user.id)
            msg = f"🔥 Все привычки выполнены! Серия: {streak} дней подряд. Ты молодец, так держать!"
        await call.message.answer(msg, parse_mode=None)


@router.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery):
    await call.answer()
    await send_stats(call.from_user.id)


@router.callback_query(F.data == "schedule")
async def cb_schedule(call: CallbackQuery):
    await call.answer()
    await send_schedule(call.from_user.id, call.message)


@router.callback_query(F.data == "set_goals")
async def cb_set_goals(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SettingsForm.goals)
    await call.message.answer(
        "Расскажи о своих целях 🎯\n"
        "Например: «высыпаться, набрать форму, меньше прокрастинировать».\n"
        "Напиши одним сообщением."
    )


@router.callback_query(F.data == "set_check")
async def cb_set_check(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SettingsForm.check)
    await call.message.answer("Во сколько мне интересоваться, как проходит день? Формат ЧЧ:ММ, например <b>14:00</b>")


@router.callback_query(F.data == "settings")
async def cb_settings(call: CallbackQuery):
    await call.answer()
    u = get_user(call.from_user.id)
    if not u:
        register_user(call.from_user.id, call.from_user.first_name or "друг")
        u = get_user(call.from_user.id)
    await call.message.answer(
        "⚙️ <b>Настройки</b>\nНажми, чтобы изменить время:", reply_markup=settings_kb(u)
    )


@router.callback_query(F.data == "set_wake")
async def cb_set_wake(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SettingsForm.wake)
    await call.message.answer("Во сколько тебя будить? Формат ЧЧ:ММ, например <b>07:00</b>")


@router.callback_query(F.data == "set_report")
async def cb_set_report(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(SettingsForm.report)
    await call.message.answer("Во сколько присылать вечерний отчёт? Формат ЧЧ:ММ, например <b>23:00</b>")


@router.message(SettingsForm.wake)
async def set_wake_value(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if not t:
        await message.answer("Не понял время. Напиши в формате ЧЧ:ММ, например 06:30")
        return
    set_user_time(message.from_user.id, "wake_time", t)
    await state.clear()
    await message.answer(f"Готово! Буду будить в {t} ⏰")


@router.message(SettingsForm.report)
async def set_report_value(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if not t:
        await message.answer("Не понял время. Напиши в формате ЧЧ:ММ, например 23:30")
        return
    set_user_time(message.from_user.id, "report_time", t)
    await state.clear()
    await message.answer(f"Готово! Вечерний отчёт в {t} 🌙")


@router.message(SettingsForm.check)
async def set_check_value(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if not t:
        await message.answer("Не понял время. Напиши в формате ЧЧ:ММ, например 14:00")
        return
    set_user_time(message.from_user.id, "check_time", t)
    await state.clear()
    await message.answer(f"Готово! Буду интересоваться твоим днём в {t} ☀️")


@router.message(SettingsForm.goals)
async def set_goals_value(message: Message, state: FSMContext):
    goals = (message.text or "").strip()
    if len(goals) < 3:
        await message.answer("Опиши цели чуть подробнее одним сообщением 🙂")
        return
    set_user_goals(message.from_user.id, goals)
    await state.clear()
    await message.answer(
        "Запомнил твои цели 🎯 Теперь учту их в советах и графике.\n"
        "Хочешь — построю распорядок дня прямо сейчас: /plan"
    )


# Любой свободный текст (вне команд и вне FSM) — общение с тренером.
@router.message(F.text & ~F.text.startswith("/"), StateFilter(None))
async def chat_with_coach(message: Message):
    register_user(message.from_user.id, message.from_user.first_name or "друг")
    try:
        await bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    ctx = user_context(message.from_user.id)
    reply = await ask_coach(message.text, ctx)

    # Fallback ответы, если ИИ не работает
    if not reply:
        text_lower = message.text.lower()
        if any(w in text_lower for w in ["помощь", "помоги", "как", "что"]):
            reply = "Помогу! 💪 Выполняй ежедневно:\n• 💧 Пить воду\n• 🤸 Зарядка 10–15 мин\n• 🚿 Холодный душ\n\nОтмечай кнопками, и я буду следить!"
        elif any(w in text_lower for w in ["привычка", "привычки", "план", "сегодня"]):
            reply = f"Вот твой план на сегодня: вода 💧, зарядка 🤸, холодный душ 🚿. {ctx}"
        elif any(w in text_lower for w in ["мотив", "вдохнови", "энергия", "сил"]):
            streak = get_streak(message.from_user.id)
            reply = f"Вот это да! {streak} дней подряд — это серьёзно 🔥 Ты уже не останавливаешься. Давай дальше!"
        else:
            reply = "Я рядом 💪 Отмечай привычки кнопками — вместе справимся!"

    await message.answer(reply, parse_mode=None, reply_markup=main_menu_kb(message.from_user.id))


# --------------------------------------------------------------------------- #
#                          ВЕБ-СЕРВЕР (для хостинга)                          #
# --------------------------------------------------------------------------- #
async def start_web():
    """Маленький HTTP-сервер для health-check на Render/Railway и анти-засыпания."""
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="Discipline bot is running ✅"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("HTTP health-check на порту %s", PORT)


# --------------------------------------------------------------------------- #
#                                   MAIN                                       #
# --------------------------------------------------------------------------- #
async def main():
    global bot
    if not BOT_TOKEN:
        raise SystemExit("❌ BOT_TOKEN не задан. Заполни .env (см. .env.example)")
    if not GOOGLE_API_KEY:
        log.warning("⚠️ GOOGLE_API_KEY не задан — тренер будет отвечать заглушками.")

    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск / перезапуск"),
            BotCommand(command="menu", description="План дня и кнопки"),
            BotCommand(command="stats", description="Статистика"),
            BotCommand(command="plan", description="Персональный график дня"),
            BotCommand(command="goals", description="Задать свои цели"),
            BotCommand(command="settings", description="Настройки времени"),
            BotCommand(command="help", description="Помощь"),
        ]
    )

    scheduler.add_job(minute_tick, CronTrigger(second=0))
    scheduler.start()
    asyncio.create_task(start_web())

    log.info("Бот запущен. TZ=%s", TZ_NAME)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен.")
