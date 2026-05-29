# -*- coding: utf-8 -*-
"""
Marsa Moments — бот Марса.

Что делает:
  Сидит в Telegram-группе и следит за событиями смены:
    • геолокации (старт смены),
    • фото (с подписями или без),
    • сеты фотографий (если меньше 10 — напоминает с разной фразой),
    • текстовые отчёты (Shift Report, End of Shift Report).
  При End of Shift Report сверяет текст и сводку событий за день — пишет вердикт.

Запуск:
  1. pip install python-telegram-bot anthropic
  2. Задать TELEGRAM_TOKEN и ANTHROPIC_API_KEY как переменные окружения.
  3. python bot.py
"""

import os
import re
import json
import random
import logging
import datetime as dt
from pathlib import Path

import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

from knowledge import (
    SYSTEM_PROMPT,
    SET_MIN_PHOTOS,
    SET_REMINDER_PHRASES,
    SHOOTING_TIPS,
    TOTAL_PRAISE_THRESHOLD,
    TOTAL_SCOLD_THRESHOLD,
    INDIVIDUAL_WEEKEND_MIN,
    INDIVIDUAL_WEEKDAY_MIN,
    WEEKEND_WEEKDAYS,
    TEAM_PRAISE_PHRASES,
    TEAM_SCOLD_PHRASES,
    INDIVIDUAL_UNDER_PHRASES,
    # новое для Этапа 1:
    EMPLOYEES,
    resolve_employee_name,
    LOCATION_KEYWORDS,
    TOPIC_KEYWORDS,
    detect_location,
    detect_topic_type,
)

# ──────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "СЮДА_ТОКЕН_ОТ_BOTFATHER")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "СЮДА_КЛЮЧ_ANTHROPIC")
# ──────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("marsa_bot")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ──────────────────────────────────────────────
# ПАМЯТЬ СОБЫТИЙ СМЕНЫ (фото, геолокации) — хранится в файле
# ──────────────────────────────────────────────

EVENTS_FILE = Path("shift_events.json")
MAX_EVENT_AGE_HOURS = 24 * 30  # 30 дней


def load_events() -> dict:
    if EVENTS_FILE.exists():
        try:
            return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Не смог прочитать shift_events.json, начинаю заново.")
    return {}


def save_events(events: dict) -> None:
    try:
        EVENTS_FILE.write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("Не смог сохранить события")


def prune_old_events(events: dict) -> dict:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=MAX_EVENT_AGE_HOURS)
    cleaned = {}
    for chat_id, evs in events.items():
        kept = []
        for e in evs:
            try:
                t = dt.datetime.fromisoformat(e["time"])
                if t >= cutoff:
                    kept.append(e)
            except Exception:
                continue
        if kept:
            cleaned[str(chat_id)] = kept
    return cleaned


def add_event(chat_id: int, event_type: str, caption: str | None, author: str) -> None:
    events = prune_old_events(load_events())
    events.setdefault(str(chat_id), []).append({
        "type": event_type,
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        "caption": caption,
        "author": author,
    })
    save_events(events)


def events_summary_for_chat(chat_id: int) -> str:
    """Сводка событий за последние 24 часа для конкретного чата."""
    events = prune_old_events(load_events())
    evs = events.get(str(chat_id), [])
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    recent = []
    for e in evs:
        try:
            t = dt.datetime.fromisoformat(e["time"])
            if t >= since:
                recent.append((t, e))
        except Exception:
            continue
    if not recent:
        return "За последние 24 часа в этом чате не зафиксировано фото/геолокаций."

    recent.sort(key=lambda x: x[0])
    photos = sum(1 for _, e in recent if e["type"] == "photo")
    locations = sum(1 for _, e in recent if e["type"] == "location")

    # Дубайское время для отображения (UTC+4)
    DUBAI_OFFSET = dt.timedelta(hours=4)

    lines = [f"Всего за 24 часа: фото — {photos}, геолокаций — {locations}.", "Хронология:"]
    for t, e in recent:
        local = (t + DUBAI_OFFSET).strftime("%H:%M")
        kind = {"photo": "📸 фото", "location": "📍 геолокация"}.get(e["type"], e["type"])
        cap = f" — подпись: «{e['caption']}»" if e.get("caption") else " — без подписи"
        lines.append(f"  • {local} от {e.get('author', '?')}: {kind}{cap}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# ДЕТЕКТОРЫ ТИПОВ СООБЩЕНИЙ
# ──────────────────────────────────────────────

# Детектор сета в подписи к фото или в тексте.
# Срабатывает на:
#   "14", "14 фото", "14 пик", "14 pics", "14 кадров"
#   "11 (by Polina)", "03 ( by Polina )"
# НЕ срабатывает на:
#   "21:00 принтеры", "Set 1: 14", "Принтеры заправлены"
# Логика: строка начинается с числа, после числа либо конец,
# либо специальное слово (фото/пик/pics/by), либо открывающая скобка.
SET_PATTERN = re.compile(
    r"^\s*0*(\d{1,3})\s*"
    r"(?:$|\s+(?:фото|пик|pics?|кадра?|кадров|by)\b|\s*\(\s*by\b)",
    re.IGNORECASE
)


def detect_set_size(text: str | None) -> int | None:
    """Если строка похожа на сет — возвращает количество фото. Иначе None."""
    if not text:
        return None
    m = SET_PATTERN.match(text.strip())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def looks_like_report(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    markers = [
        "shift report",
        "end of shift",
        "total revenue",
        "sales breakdown",
        "equipment list",
        "photoshoot sets",
        "remaining consumables",
        "individual sales",
    ]
    return any(m in t for m in markers)


def is_end_of_shift(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return "end of shift" in t or "total revenue" in t


# ──────────────────────────────────────────────
# ПАРСИНГ ОТЧЁТА И ОБРАТНАЯ СВЯЗЬ ПО ПРОДАЖАМ
# ──────────────────────────────────────────────

TOTAL_REVENUE_RE = re.compile(r"Total Revenue:\s*([\d\s,]+)", re.IGNORECASE)
INDIVIDUAL_SALES_RE = re.compile(
    r"Photographer\s+([^:]+?):\s*([\d\s,]+)\s*AED", re.IGNORECASE
)
DATE_RE = re.compile(
    r"Date:?\s*(\d{1,2})[\-\.\/](\d{1,2})[\-\.\/](\d{2,4})", re.IGNORECASE
)


def _digits(s: str) -> int | None:
    """Извлекает число из строки вида '2 300 AED' → 2300."""
    cleaned = re.sub(r"\D", "", s or "")
    return int(cleaned) if cleaned else None


def parse_total_revenue(text: str) -> int | None:
    m = TOTAL_REVENUE_RE.search(text)
    if not m:
        return None
    return _digits(m.group(1))


def parse_individual_sales(text: str) -> dict[str, int]:
    """Возвращает словарь {имя_фотографа: продажи_в_AED}.
    Ищем только в секции Individual Sales (не в Salaries и не в Sales Breakdown)."""
    result = {}
    # Берём кусок текста от "Individual Sales" до "Salaries" или "Expenses"
    individual_block = re.search(
        r"Individual\s+Sales\s*:?(.*?)(?:Salaries|Expenses|Defective|Remaining|$)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not individual_block:
        return result
    block_text = individual_block.group(1)
    for m in INDIVIDUAL_SALES_RE.finditer(block_text):
        name = m.group(1).strip()
        amount = _digits(m.group(2))
        if amount is not None:
            result[name] = amount
    return result


def parse_report_date(text: str) -> dt.date | None:
    m = DATE_RE.search(text)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        if year < 100:
            year += 2000
        return dt.date(year, month, day)
    except (ValueError, OverflowError):
        return None


def build_performance_feedback(text: str) -> str:
    """Возвращает доп. строчки для вердикта: похвала/мягкая ругань/недотяги по фотографам.
    Если повода нет — пустая строка."""
    extras = []

    revenue = parse_total_revenue(text)
    if revenue is not None:
        if revenue >= TOTAL_PRAISE_THRESHOLD:
            extras.append(random.choice(TEAM_PRAISE_PHRASES).format(revenue=revenue))
        elif revenue < TOTAL_SCOLD_THRESHOLD:
            extras.append(random.choice(TEAM_SCOLD_PHRASES).format(revenue=revenue))

    individuals = parse_individual_sales(text)
    report_date = parse_report_date(text)
    if individuals and report_date:
        is_weekend = report_date.weekday() in WEEKEND_WEEKDAYS
        target = INDIVIDUAL_WEEKEND_MIN if is_weekend else INDIVIDUAL_WEEKDAY_MIN
        day_label = "выходных" if is_weekend else "будней"
        for name, sales in individuals.items():
            if sales < target:
                extras.append(random.choice(INDIVIDUAL_UNDER_PHRASES).format(
                    name=name, sales=sales, target=target, day_label=day_label
                ))

    return "\n".join(extras)


# ──────────────────────────────────────────────
# ВЫЗОВ CLAUDE
# ──────────────────────────────────────────────

def check_report(report_text: str, chat_id: int) -> str:
    try:
        extra = ""
        if is_end_of_shift(report_text):
            extra = (
                "\n\n---\n"
                "СВОДКА СОБЫТИЙ ЭТОГО ЧАТА ЗА ПОСЛЕДНИЕ 24 ЧАСА (от бота):\n"
                + events_summary_for_chat(chat_id)
                + "\n---\n"
                "Используй эту сводку, чтобы проверить отправку фото уровня чернил "
                "(начало и конец смены), батарейки на зарядке, рабочий стол, полки, "
                "скриншот WhatsApp, геолокацию в начале смены. "
                "Чего не было — отметь как нарушение."
            )

        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": report_text + extra}],
        )
        parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
        verdict = "\n".join(parts).strip() or "Не удалось разобрать ответ модели."

        # Если это End of Shift — добавляем похвалу/мягкую ругань/недотяги
        if is_end_of_shift(report_text):
            feedback = build_performance_feedback(report_text)
            if feedback:
                verdict = verdict + "\n\n" + feedback

        return verdict
    except Exception as e:
        logger.exception("Ошибка при запросе к Claude")
        return f"⚠️ Не смогла проверить отчёт (техническая ошибка): {e}"


# ──────────────────────────────────────────────
# ОБРАБОТЧИКИ
# ──────────────────────────────────────────────

def author_of(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "неизвестно"
    if u.username:
        return f"@{u.username}"
    return u.full_name or "неизвестно"


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.text:
        return

    # Запоминаем место (автораспознавание точки и темы)
    recognize_place(update)

    text = msg.text

    # 1. Это сет? — реагируем мгновенно, без Claude
    n = detect_set_size(text)
    if n is not None:
        if n < SET_MIN_PHOTOS:
            await msg.reply_text(make_set_reminder(n))
        return  # сет обработан, дальше не идём

    # 2. Это отчёт?
    if looks_like_report(text):
        logger.info("Получен отчёт, отправляю на проверку...")
        chat_id = update.effective_chat.id if update.effective_chat else 0
        verdict = check_report(text, chat_id)
        await msg.reply_text(verdict)
        return

    # 3. Иначе — болтовня, молчим


def make_set_reminder(n: int) -> str:
    """Собирает сообщение про маленький сет.
    50% случаев — короткое напоминание.
    50% случаев — напоминание + совет по съёмке (как разговорить гостя)."""
    reminder = random.choice(SET_REMINDER_PHRASES).format(n=n)
    if random.random() < 0.5:
        tip = random.choice(SHOOTING_TIPS)
        return f"{reminder}\n\n{tip}"
    return reminder


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает фото. Если у фото есть подпись и она похожа на сет —
    проверяем размер и при необходимости пишем напоминание."""
    msg = update.effective_message
    if msg is None or not msg.photo:
        return
    chat = update.effective_chat
    if not chat:
        return

    # Запоминаем место
    recognize_place(update)

    caption = msg.caption
    add_event(chat.id, "photo", caption, author_of(update))
    logger.info(f"Фото в чате {chat.id} от {author_of(update)} (подпись: {caption!r})")

    # Если в подписи распознался сет — проверяем размер
    n = detect_set_size(caption)
    if n is not None and n < SET_MIN_PHOTOS:
        await msg.reply_text(make_set_reminder(n))


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.location:
        return
    chat = update.effective_chat
    if not chat:
        return

    # Запоминаем место
    recognize_place(update)

    add_event(chat.id, "location", None, author_of(update))
    logger.info(f"Геолокация в чате {chat.id} от {author_of(update)}")


async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /whereami — отладочная. Показывает что Марса знает про текущее место."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    location, topic_type = recognize_place(update)

    lines = ["📍 Где я нахожусь:"]
    lines.append(f"• Group: {chat.title or '(без названия)'}")
    lines.append(f"• Распознала точку: **{location or 'не распознала'}**")
    lines.append(f"• Распознала тему: **{topic_type or 'не распознала (или основной чат)'}**")
    await msg.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_locations(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /locations — список всех мест, которые Марса видела."""
    places = load_known_places()
    if not places:
        await update.effective_message.reply_text(
            "Пока ни одной локации не зафиксировала. Напиши /whereami в нужном чате."
        )
        return

    lines = ["📋 Места, которые я знаю:", ""]
    # Группируем по точкам
    by_location = {}
    for key, info in places.items():
        loc = info.get("location") or "неизвестно"
        by_location.setdefault(loc, []).append(info)

    for loc, items in sorted(by_location.items()):
        lines.append(f"🏢 **{loc}**:")
        for it in items:
            topic = it.get("topic_type") or "основной чат"
            title = it.get("group_title", "")
            topic_title = it.get("topic_title", "")
            lines.append(f"  • {topic} (в группе «{title}»{', тема «' + topic_title + '»' if topic_title else ''})")
        lines.append("")

    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# ──────────────────────────────────────────────
# РАСПОЗНАВАНИЕ И ЗАПОМИНАНИЕ МЕСТ
# ──────────────────────────────────────────────

KNOWN_PLACES_FILE = Path("known_places.json")


def load_known_places() -> dict:
    if KNOWN_PLACES_FILE.exists():
        try:
            return json.loads(KNOWN_PLACES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_known_places(places: dict) -> None:
    try:
        KNOWN_PLACES_FILE.write_text(
            json.dumps(places, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        logger.exception("Не смог сохранить known_places")


def get_topic_title(msg) -> str | None:
    """Пытается достать название темы из сообщения, если оно в форум-теме."""
    try:
        if getattr(msg, "is_topic_message", False):
            reply = msg.reply_to_message
            if reply and getattr(reply, "forum_topic_created", None):
                return reply.forum_topic_created.name
    except Exception:
        pass
    return None


def recognize_place(update: Update) -> tuple[str | None, str | None]:
    """Смотрит на чат/тему обновления, возвращает (location, topic_type).
    Заодно запоминает место в known_places.json."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat:
        return (None, None)

    group_title = chat.title
    location = detect_location(group_title)

    topic_title = get_topic_title(msg) if msg else None
    thread_id = getattr(msg, "message_thread_id", None) if msg else None
    topic_type = detect_topic_type(topic_title)

    # Запоминаем — ключ "chat_id:thread_id" (thread_id=0 для основного чата)
    key = f"{chat.id}:{thread_id or 0}"
    places = load_known_places()
    places[key] = {
        "chat_id": chat.id,
        "thread_id": thread_id,
        "group_title": group_title,
        "topic_title": topic_title,
        "location": location,
        "topic_type": topic_type,
    }
    save_known_places(places)

    return (location, topic_type)


# ──────────────────────────────────────────────

def main() -> None:
    if "СЮДА_" in TELEGRAM_TOKEN or "СЮДА_" in ANTHROPIC_API_KEY:
        raise SystemExit(
            "Сначала задай TELEGRAM_TOKEN и ANTHROPIC_API_KEY "
            "(переменные окружения или в коде)."
        )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("whereami", cmd_whereami))
    app.add_handler(CommandHandler("locations", cmd_locations))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))

    logger.info("Марса запущена. Жду события смены...")
    app.run_polling()


if __name__ == "__main__":
    main()
