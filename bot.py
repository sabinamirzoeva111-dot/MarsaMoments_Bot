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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

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
    GREETINGS_START,
    GREETINGS_REPORT_START,
    SALARY_QUIPS,
    # новое для Этапа 1:
    EMPLOYEES,
    resolve_employee_name,
    find_employee_by_name,
    LOCATION_KEYWORDS,
    TOPIC_KEYWORDS,
    SHIFT_START_HOURS,
    PRINTERS_DEADLINE_MINUTES,
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


def deterministic_sales_breakdown_check(text: str) -> str | None:
    """Детерминированная проверка Sales Breakdown без LLM.
    Возвращает текст вердикта или None если требует анализа Claude.

    Применяется только если сообщение — преимущественно Sales Breakdown
    (нет блоков Total Revenue, Cash Holding и т.д.)."""
    t = text.lower()

    # Применяем только если это короткий отчёт с Sales Breakdown
    has_sales = "sales breakdown" in t or bool(re.search(r"\d+\s*aed", text, re.IGNORECASE))
    has_other_blocks = any(m in t for m in [
        "total revenue", "card:", "cash:", "cash holding",
        "individual sales", "salaries", "defective", "remaining",
        "expenses", "photoshoot sets",
    ])
    if not has_sales or has_other_blocks:
        return None  # пусть Claude разбирает полноценный отчёт

    # Парсим каждую строку
    lines = split_sales_breakdown(text)
    if not lines:
        return None

    issues_by_type = {}  # тип -> список номеров строк

    for i, raw_line in enumerate(lines, 1):
        parsed = parse_sale_line(raw_line)
        if not parsed:
            continue
        line_lower = raw_line.lower()

        # Чаевые не проверяем
        if parsed["is_tip"]:
            continue

        # 1) Business Card with Envelope — должна быть всегда
        if not re.search(r"business\s*card", line_lower):
            issues_by_type.setdefault("no_business_card", []).append(i)

        # 2) Имя гостя — после телефона ИЛИ после "No Need Digital"
        # Грубая эвристика: есть ли что-то осмысленное после телефона/no need digital
        no_digital = bool(re.search(r"no\s*need\s*digital|без\s*цифров|не\s*нужн.*электрон|no\s*electronic", line_lower))
        if not parsed["phone"] and not no_digital:
            issues_by_type.setdefault("no_phone", []).append(i)
        elif not parsed.get("guest") and not no_digital:
            # У нас есть телефон, но не вытащилось имя гостя — пропустим, parse_sale_line часто это не вытаскивает корректно
            pass

    if not issues_by_type:
        # Все ОК — чистый отчёт
        return "✅ Report is clean / Отчёт чистый"

    # Формируем вердикт
    lines_out = ["⚠️ Found issues:"]
    if "no_business_card" in issues_by_type:
        nums = ", ".join(str(n) for n in issues_by_type["no_business_card"])
        lines_out.append(f"• Lines {nums}: missing Business Card with Envelope")
    if "no_phone" in issues_by_type:
        nums = ", ".join(str(n) for n in issues_by_type["no_phone"])
        lines_out.append(f"• Lines {nums}: missing guest phone number")
    lines_out.append("———")
    lines_out.append("⚠️ Нашёл проблемы:")
    if "no_business_card" in issues_by_type:
        nums = ", ".join(str(n) for n in issues_by_type["no_business_card"])
        lines_out.append(f"• В строках {nums} нет Business Card with Envelope")
    if "no_phone" in issues_by_type:
        nums = ", ".join(str(n) for n in issues_by_type["no_phone"])
        lines_out.append(f"• В строках {nums} нет номера телефона гостя")
    return "\n".join(lines_out)


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
        return f"⚠️ Не смог проверить отчёт (техническая ошибка): {e}"


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
    location, topic_type = recognize_place(update)

    # В Instructions & Reminders — молчим
    if topic_type == "instructions":
        return

    text = msg.text

    # Детект Shift Report (открытие смены) — отдельная проверка структуры
    if is_shift_report_open(text):
        chat_id = update.effective_chat.id if update.effective_chat else 0
        if chat_id:
            add_event(chat_id, "shift_report_open", text[:500], author_of(update))
        verdict = check_shift_report_structure(text, location)
        if verdict is None:
            # Всё чисто — реакция
            await react_clean(context, chat_id, msg.message_id)
        else:
            await msg.reply_text(verdict)
        return

    # Детект строк Sales Breakdown по ходу смены (с иконкой 💳 или 💵)
    if is_sale_line(text):
        chat_id = update.effective_chat.id if update.effective_chat else 0
        if chat_id:
            add_event(chat_id, "sale_line", text, author_of(update))

    # 0. Это reply на сообщение Марсы? — обработка объяснения
    if msg.reply_to_message and msg.reply_to_message.from_user:
        bot_id = context.bot.id
        if msg.reply_to_message.from_user.id == bot_id:
            await handle_explanation_reply(update, context)
            return

    # 1. Это сет? — реагируем мгновенно, без Claude
    n = detect_set_size(text)
    if n is not None:
        chat_id = update.effective_chat.id if update.effective_chat else 0
        if chat_id:
            add_event(chat_id, "set", f"size={n}", author_of(update))
        if n < SET_MIN_PHOTOS:
            await msg.reply_text(make_set_reminder(n))
        return

    # 2. Это отчёт?
    if looks_like_report(text):
        chat_id = update.effective_chat.id if update.effective_chat else 0

        verdict = deterministic_sales_breakdown_check(text)
        if verdict is None:
            logger.info("Получен отчёт, отправляю на проверку Claude...")
            verdict = check_report(text, chat_id)
        else:
            logger.info("Детерминированная проверка Sales Breakdown")

        save_last_report(update.effective_chat.id, text, verdict)
        if is_clean_verdict(verdict):
            await react_clean(context, chat_id, msg.message_id)
        else:
            await msg.reply_text(verdict)
        return

def is_clean_verdict(verdict: str) -> bool:
    """Определяет, чистый ли отчёт по вердикту."""
    v = (verdict or "").lower()
    clean_markers = [
        "report is clean",
        "отчёт чистый",
        "отчет чистый",
        "all good",
        "no issues",
        "проблем не нашёл",
        "ошибок нет",
    ]
    return any(m in v for m in clean_markers)

    # 3. Иначе — болтовня, молчим


# ──────────────────────────────────────────────
# REPLY-ДИАЛОГ: ретушёр объясняет ситуацию по ошибке
# ──────────────────────────────────────────────

LAST_REPORTS_FILE = Path("last_reports.json")


def save_last_report(chat_id: int, report_text: str, verdict: str) -> None:
    """Сохраняет последний отчёт и вердикт в этом чате для контекста объяснений."""
    try:
        data = {}
        if LAST_REPORTS_FILE.exists():
            try:
                data = json.loads(LAST_REPORTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[str(chat_id)] = {
            "report": report_text,
            "verdict": verdict,
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        LAST_REPORTS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не смог сохранить last_reports")


def get_last_report(chat_id: int) -> dict | None:
    """Возвращает последний отчёт+вердикт для чата (если был в последние 6 часов)."""
    if not LAST_REPORTS_FILE.exists():
        return None
    try:
        data = json.loads(LAST_REPORTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    entry = data.get(str(chat_id))
    if not entry:
        return None
    try:
        t = dt.datetime.fromisoformat(entry["time"])
    except Exception:
        return None
    if dt.datetime.now(dt.timezone.utc) - t > dt.timedelta(hours=6):
        return None
    return entry


async def handle_explanation_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Когда сотрудник ответил reply на сообщение Марсы — учитываем объяснение."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    explanation = msg.text or ""
    bot_message_text = msg.reply_to_message.text or ""

    # Достаём последний отчёт в этом чате (для контекста)
    last = get_last_report(chat.id)

    # Формируем промпт для Claude — пересмотр с учётом объяснения
    user_prompt = (
        "В предыдущем сообщении я (Марса) написал следующее по поводу отчёта:\n\n"
        f"{bot_message_text}\n\n"
        f"Сотрудник ответил с объяснением:\n\n«{explanation}»\n\n"
    )
    if last:
        user_prompt += f"Оригинальный отчёт был такой:\n\n{last['report']}\n\n"
    user_prompt += (
        "Учти объяснение. Если оно убедительно объясняет одну из проблем "
        "(например: «забыл конверт, отдам в следующий раз», «гость отказался от номера», "
        "«опечатка в номере»), то признай это и обнови свой вердикт — убери эту проблему "
        "из списка, кратко скажи что принял объяснение. "
        "Если в отчёте всё ещё остаются проблемы — перечисли только оставшиеся. "
        "Если объяснение не убирает проблемы — мягко скажи об этом. "
        "Будь кратким, по делу, без преамбул."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        reply_text = response.content[0].text.strip()
    except Exception as e:
        logger.exception("Ошибка при обработке объяснения")
        reply_text = f"Не смог обработать объяснение (техническая ошибка): {e}"

    await msg.reply_text(reply_text)


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
    """Обрабатывает фото. Если в Shifts фото принтера — запускает vision-проверку."""
    msg = update.effective_message
    if msg is None or not msg.photo:
        return
    chat = update.effective_chat
    if not chat:
        return

    # Запоминаем место
    location, topic_type = recognize_place(update)

    # В Instructions & Reminders — молчим
    silent = (topic_type == "instructions")

    caption = msg.caption
    add_event(chat.id, "photo", caption, author_of(update))
    artifact = detect_shift_close_artifact(caption)
    if artifact:
        add_event(chat.id, f"artifact_{artifact}", caption, author_of(update))
    logger.info(f"Фото в чате {chat.id} от {author_of(update)} (подпись: {caption!r}, артефакт: {artifact})")

    if silent:
        return

    # Если подпись распознана как сет — обработать
    n = detect_set_size(caption)
    if n is not None:
        add_event(chat.id, "set", f"size={n}", author_of(update))
        if n < SET_MIN_PHOTOS:
            await msg.reply_text(make_set_reminder(n))
        return

    # Vision-проверка принтера: если в Shifts и подпись намекает на принтер
    if topic_type == "shifts" and artifact == "printer":
        await check_printer_photo_with_vision(update, context, location)
        return


PRINTER_INK_KEYWORDS_HINT = ["L8050", "EcoTank", "Epson"]


async def check_printer_photo_with_vision(update: Update, context: ContextTypes.DEFAULT_TYPE, location: str | None) -> None:
    """Через Claude Vision проверяет: на фото принтер(ы), видны ли баки с чернилами, и в них есть чернила."""
    msg = update.effective_message
    if not msg or not msg.photo:
        return
    chat = update.effective_chat

    try:
        # Берём самое крупное фото
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        # Скачиваем во временный файл
        import tempfile, base64
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_path = f.name
        await file.download_to_drive(tmp_path)
        with open(tmp_path, "rb") as f:
            img_bytes = f.read()
        img_b64 = base64.standard_b64encode(img_bytes).decode("utf-8")

        # Эталон принтеров для этой точки
        expected_count = PRINTER_COUNT_BY_LOCATION.get(location or "") if location else None
        expected_hint = f"\nExpected printer count for this location: {expected_count}." if expected_count else ""

        prompt = (
            "На фото — фото принтера(ов) для отчёта о начале смены в Marsa Moments.\n"
            "Оцени:\n"
            "1. Сколько принтеров видно на фото? Это Epson EcoTank L8050.\n"
            "2. Видны ли прозрачные баки с чернилами на принтерах?\n"
            "3. По каждому видимому баку: уровень чернил выше 50%, 25-50%, или ниже 25%?\n"
            f"{expected_hint}\n\n"
            "Ответ строго в JSON:\n"
            "{\n"
            '  "printers_visible": <число>,\n'
            '  "tanks_visible": <true/false>,\n'
            '  "low_ink": <true/false>,  // если хоть один бак ниже 25%\n'
            '  "comment": "<краткое наблюдение, 1 строка>"\n'
            "}\n"
            "Никакого текста кроме JSON."
        )

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        raw = response.content[0].text.strip()
        # Чистим возможные тройные кавычки
        raw_clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        result = json.loads(raw_clean)

        issues = []
        if expected_count and result.get("printers_visible", 0) < expected_count:
            issues.append(
                f"• На фото видно {result.get('printers_visible')} принтер(ов), "
                f"а для {location} должно быть {expected_count}. Сделай общее фото где видно все."
            )
        if not result.get("tanks_visible"):
            issues.append("• Баки с чернилами не видны. Сделай фото так чтобы баки были в кадре.")
        if result.get("low_ink"):
            issues.append("• Уровень чернил в одном из баков низкий — пора заправлять.")

        if not issues:
            # Всё ок — реакция
            await react_clean(context, chat.id, msg.message_id)
            # Сохраняем подтверждённое событие
            add_event(chat.id, "artifact_printer_verified", raw_clean, author_of(update))
        else:
            await msg.reply_text("📸 Фото принтера — есть замечания:\n" + "\n".join(issues))

        try:
            os.remove(tmp_path)
        except Exception:
            pass

    except Exception as e:
        logger.exception("Vision-проверка принтера упала")
        # Не флудим в чат, просто ставим реакцию что фото получили
        await react_clean(context, chat.id, msg.message_id)


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None or not msg.location:
        return
    chat = update.effective_chat
    if not chat:
        return

    # Запоминаем место
    location, topic_type = recognize_place(update)

    # live_period в секундах: 0 / None = обычная точка; ≥900 = онлайн от 15 мин
    live_period = getattr(msg.location, "live_period", None) or 0
    is_online_15min = live_period >= 900

    add_event(chat.id, "location", f"live_period={live_period}", author_of(update))
    logger.info(f"Геолокация в чате {chat.id} от {author_of(update)} live={live_period}")

    # В Instructions & Reminders — молчим дальше
    if topic_type == "instructions":
        return

    # Если это не онлайн ≥15 мин — просим переслать правильно
    if not is_online_15min:
        try:
            await msg.reply_text(
                "Нужна онлайн геолокация от 15 минут, не точка."
            )
        except Exception:
            logger.exception("Не смог попросить онлайн геолокацию")
        return

    # Это онлайн ≥15 мин — теперь смотрим вовремя ли
    if not location:
        # Точку не распознали, но онлайн принимаем — просто лайк
        await react_clean(context, chat.id, msg.message_id)
        return

    shift_start = SHIFT_START_HOURS.get(location)
    if shift_start is None:
        await react_clean(context, chat.id, msg.message_id)
        return

    # Dubai UTC+4
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_dubai = now_utc + dt.timedelta(hours=4)
    now_h, now_m = now_dubai.hour, now_dubai.minute

    # Считаем сколько минут после старта смены
    minutes_after_start = (now_h - shift_start) * 60 + now_m

    # Допуск 5 минут
    LATENESS_GRACE = 5

    if minutes_after_start <= LATENESS_GRACE:
        # Вовремя (включая допуск 5 минут) → лайк
        await react_clean(context, chat.id, msg.message_id)
        return

    # Опоздание
    user = update.effective_user
    username = (user.username if user else None) or f"id{user.id if user else 0}"
    real_name = resolve_employee_name(username) or username
    count = record_lateness(username, location, minutes_after_start)
    await handle_lateness(msg, real_name, count, minutes_after_start, location, context)


# Разрешённые реакции на чистые сообщения (Марса выбирает любую)
ALLOWED_CLEAN_REACTIONS = ["👍", "❤️", "✅", "✔️", "☑️"]


async def react_clean(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    """Ставит реакцию из разрешённого набора. Если не получилось — молчит."""
    reaction = random.choice(ALLOWED_CLEAN_REACTIONS)
    try:
        await context.bot.set_message_reaction(
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )
    except Exception:
        logger.exception(f"Не смог поставить реакцию {reaction}")


LATENESS_FILE = Path("lateness.json")


def record_lateness(username: str, location: str, minutes_late: int) -> int:
    """Записывает опоздание сотрудника. Счётчик за ТЕКУЩУЮ календарную НЕДЕЛЮ.
    Возвращает счётчик опозданий за эту неделю."""
    try:
        data = {}
        if LATENESS_FILE.exists():
            try:
                data = json.loads(LATENESS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}

        # Ключ по году+номеру недели (ISO) — обнуляется каждую неделю
        today = dt.date.today()
        year, week, _ = today.isocalendar()
        key = f"{username}::{year}-W{week:02d}"

        entry = data.get(key, {"count": 0, "incidents": []})
        entry["count"] += 1
        entry["incidents"].append({
            "time": dt.datetime.now(dt.timezone.utc).isoformat(),
            "location": location,
            "minutes_late": minutes_late,
        })
        data[key] = entry

        LATENESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return entry["count"]
    except Exception:
        logger.exception("Не смог записать опоздание")
        return 0


async def handle_lateness(msg, real_name: str, count: int, minutes_late: int, location: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Реагирует на опоздание по новой логике:
    - до +5 мин   → норма, эта функция не вызывается
    - +6..15 мин  → 1-й: мягко / 2-й: «я слежу» / 3-й за неделю: штраф 50 + GM
    - +15+ мин    → жёстко с первого раза + GM с первого раза"""
    HARD_THRESHOLD = 15

    if minutes_late > HARD_THRESHOLD:
        # Жёстко с первого раза
        text = (
            f"⏰ {real_name}, опоздание {minutes_late} минут от начала смены — "
            f"это много. Штраф 50 AED, инцидент уйдёт GM."
        )
        await _send_safe(msg, text)
        await send_to_gm(
            context,
            f"⚠️ Жёсткое опоздание\n"
            f"Сотрудник: {real_name}\n"
            f"Точка: {location}\n"
            f"Опоздание: {minutes_late} мин (порог жёсткой зоны: >{HARD_THRESHOLD} мин)\n"
            f"Штраф: 50 AED",
        )
        return

    # Зона +6..15 минут — мягкая эскалация
    if count == 1:
        text = (
            f"⏰ {real_name}, ты опоздала(а) на {minutes_late} минут от начала смены. "
            f"В этот раз — мягко, но постарайся приходить вовремя."
        )
    elif count == 2:
        text = (
            f"⏰ {real_name}, снова опоздание ({minutes_late} мин). "
            f"Это уже второй раз за неделю — я слежу за тобой."
        )
    else:
        # 3+ за неделю — штраф + GM
        text = (
            f"⏰ {real_name}, опоздание {minutes_late} мин. "
            f"Это {count}-й раз за неделю — штраф 50 AED."
        )
        await send_to_gm(
            context,
            f"⚠️ Опоздание (3+ за неделю)\n"
            f"Сотрудник: {real_name}\n"
            f"Точка: {location}\n"
            f"Опоздание: {minutes_late} мин\n"
            f"Случай №{count} за эту неделю\n"
            f"Штраф: 50 AED",
        )

    await _send_safe(msg, text)


async def _send_safe(msg, text: str) -> None:
    try:
        await msg.reply_text(text)
    except Exception:
        logger.exception("Не смог отправить уведомление")


def is_shift_report_open(text: str | None) -> bool:
    """Распознаёт Shift Report (открытие смены)."""
    if not text:
        return False
    t = text.lower()
    return "shift report" in t


# Эталоны принтеров по точкам
PRINTER_COUNT_BY_LOCATION = {
    "Avenue":   2,
    "O Lounge": 3,
    "Chayka":   3,
    "Molodost": 2,
    "Del Mar":  2,
    "TEST":     2,  # для теста
}

# Дни недели когда точка НЕ работает (0=пн, 1=вт, ..., 6=вс)
LOCATION_DAYS_OFF = {
    "Chayka":   [0, 1],  # пн, вт
    "Molodost": [0, 1],
    "Del Mar":  [0, 1],
}


def parse_shift_report_date(text: str) -> dt.date | None:
    """Парсит дату в формате Д.М.Г или Д/М/Г (НЕ американский)."""
    # ищем дату ДД.ММ.ГГГГ или ДД/ММ/ГГГГ
    m = re.search(r"\b(\d{1,2})[.\/](\d{1,2})[.\/](\d{4})\b", text)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        return dt.date(year, month, day)
    except (ValueError, TypeError):
        return None


def check_shift_report_structure(text: str, location: str | None) -> str | None:
    """Проверяет Shift Report по новому шаблону.
    Возвращает None если всё ок (тогда ставится реакция),
    или текст с проблемами если есть ошибки."""
    issues = []

    # 1) Дата
    date_in_report = parse_shift_report_date(text)
    today = smart_shift_date()
    if not date_in_report:
        issues.append("• Не вижу дату (формат Д.М.Г или Д/М/Г)")
    elif date_in_report != today:
        issues.append(
            f"• Дата в отчёте {date_in_report.strftime('%d.%m.%Y')} "
            f"не совпадает с сегодняшней сменой ({today.strftime('%d.%m.%Y')})"
        )

    # 2) Team
    if not re.search(r"team\s*:", text, re.IGNORECASE):
        issues.append("• Не вижу строку «Team:» с именами")

    # 3) Три фото-метки
    photo_lines = re.findall(r"photo\s*\d", text, re.IGNORECASE)
    if len(photo_lines) < 3:
        issues.append(f"• Нашёл только {len(photo_lines)} строк «Photo N» (нужно 3)")

    # 4) Stock at start of shift
    if not re.search(r"stock\s*at\s*start", text, re.IGNORECASE):
        issues.append("• Не вижу блок «Stock at start of shift»")
    else:
        # Проверяем что есть конкретные позиции с цифрами
        required = ["a4 paper", "envelopes", "bags", "frames", "business cards", "bc envelopes"]
        missing = []
        for label in required:
            if not re.search(rf"{label}\s*:\s*\d+", text, re.IGNORECASE):
                missing.append(label.title())
        if missing:
            issues.append(f"• В Stock не заполнены: {', '.join(missing)}")

    # 5) Issues
    issues_match = re.search(r"issues\s*:\s*(.+?)(?:\n\n|\Z)", text, re.IGNORECASE | re.DOTALL)
    if issues_match:
        issues_text = issues_match.group(1).strip().lower()
        # Если "none" / "нет" / "нет проблем" — всё ок. Иначе — тревога
        if issues_text not in ("none", "нет", "no", "нет проблем", "—", "-"):
            issues.append(f"⚠️ Есть отклонения в смене — сообщи GM: «{issues_match.group(1).strip()}»")

    # 6) День недели — точка работает?
    if location and location in LOCATION_DAYS_OFF:
        weekday = today.weekday()
        if weekday in LOCATION_DAYS_OFF[location]:
            day_names = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
            issues.append(
                f"• {location} обычно не работает в {day_names[weekday]}. "
                f"Уточни у GM, действительно ли сегодня смена."
            )

    # 7) Низкие остатки рамок
    frames_m = re.search(r"frames\s*:\s*(\d+)", text, re.IGNORECASE)
    if frames_m:
        frames_count = int(frames_m.group(1))
        if frames_count < 15:
            issues.append(f"• Рамок осталось мало: {frames_count} шт. Подвози скорее.")

    if not issues:
        return None

    lines = ["⚠️ Shift Report — проверь:"]
    lines.extend(issues)
    return "\n".join(lines)


def is_printer_photo(caption: str | None) -> bool:
    """Распознаёт фото принтера по подписи или по факту что подпись пустая
    и фото похоже на принтер (доверяем контексту чата Shifts)."""
    if not caption:
        # Пустые фото в Shifts во время смены тоже считаем как фото оборудования
        # (на ранних этапах смены чаще всего постят именно принтеры)
        return False
    text = caption.lower()
    keywords = [
        "принтер", "printer", "чернил", "ink", "level",
        "epson", "уровень чернил", "ink level", "tank",
        "l8050", "картридж", "cartridge",
        "заправ",  # "заправлены", "заправил"
    ]
    return any(kw in text for kw in keywords)


def is_battery_photo(caption: str | None) -> bool:
    """Распознаёт фото батареек на зарядке."""
    if not caption:
        return False
    t = caption.lower()
    return any(kw in t for kw in [
        "батарей", "battery", "batteries", "акку", "charging",
        "зарядк", "заряжа", "charger", "charge",
    ])


def is_desk_photo(caption: str | None) -> bool:
    """Распознаёт фото рабочего стола."""
    if not caption:
        return False
    t = caption.lower()
    return any(kw in t for kw in [
        "рабоч", "стол", "desk", "workplace", "workspace", "стол чистый",
        "место чисто", "рабочее место",
    ])


def is_shelves_photo(caption: str | None) -> bool:
    """Распознаёт фото полок с расходниками."""
    if not caption:
        return False
    t = caption.lower()
    return any(kw in t for kw in [
        "полк", "shelves", "shelf", "расходник", "materials",
        "бумаг", "paper stock", "supplies",
    ])


def is_whatsapp_screenshot(caption: str | None) -> bool:
    """Распознаёт скриншот WhatsApp отправки фото гостям."""
    if not caption:
        return False
    t = caption.lower()
    return any(kw in t for kw in [
        "whatsapp", "ватсап", "отправ", "sent", "delivery", "доставлен",
        "скриншот", "screenshot",
    ])


def detect_shift_close_artifact(caption: str | None) -> str | None:
    """Возвращает тип артефакта закрытия смены: printer / battery / desk / shelves / whatsapp / None."""
    if is_printer_photo(caption):
        return "printer"
    if is_battery_photo(caption):
        return "battery"
    if is_desk_photo(caption):
        return "desk"
    if is_shelves_photo(caption):
        return "shelves"
    if is_whatsapp_screenshot(caption):
        return "whatsapp"
    return None


async def check_shift_opening_deadline(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяет открытие смены: геолокация + Shift Report + фото принтера."""
    job = context.job
    chat_id = job.data["chat_id"]
    location = job.data["location"]
    thread_id = job.data.get("thread_id")

    # Если точка не работает в этот день — пропускаем проверку
    today = dt.date.today()
    if location in LOCATION_DAYS_OFF and today.weekday() in LOCATION_DAYS_OFF[location]:
        logger.info(f"{location} не работает {today.strftime('%A')}, проверка пропущена")
        return

    events = load_events().get(str(chat_id), [])
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=4)

    has_location = False
    has_shift_report = False
    has_printer_photo = False
    for e in events:
        try:
            t = dt.datetime.fromisoformat(e["time"])
        except Exception:
            continue
        if t < cutoff:
            continue
        if e.get("type") == "location":
            has_location = True
        if e.get("type") == "shift_report_open":
            has_shift_report = True
        if e.get("type") in ("artifact_printer", "artifact_printer_verified"):
            has_printer_photo = True

    missing = []
    if not has_location:
        missing.append("• онлайн геолокацию (от 15 минут)")
    if not has_shift_report:
        missing.append("• Shift Report")
    if not has_printer_photo:
        missing.append("• фото уровня чернил принтеров")

    if not missing:
        logger.info(f"Открытие смены ок ({location})")
        return

    try:
        text = "⏰ Открытие смены не закрыто. Не вижу:\n" + "\n".join(missing)
        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
        )
        logger.info(f"Напоминание об открытии смены в чат {chat_id} ({location}): {missing}")
        # И в GM
        await send_to_gm(
            context,
            f"⚠️ Открытие смены {location} не закрыто к дедлайну:\n" + "\n".join(missing),
        )
    except Exception:
        logger.exception(f"Не смог отправить напоминание в {chat_id}")


def schedule_printers_check(app, chat_id: int, location: str, thread_id: int | None = None) -> None:
    """Планирует ежедневную проверку открытия смены для указанной точки.
    Время дедлайна = SHIFT_START_HOURS[location]:30 локального времени Дубая (UTC+4)."""
    start_hour = SHIFT_START_HOURS.get(location)
    if start_hour is None:
        return

    # Дубай = UTC+4. Render контейнер в UTC.
    deadline_local_hour = start_hour
    deadline_local_minute = PRINTERS_DEADLINE_MINUTES
    deadline_utc_hour = (deadline_local_hour - 4) % 24
    deadline_time = dt.time(hour=deadline_utc_hour, minute=deadline_local_minute, tzinfo=dt.timezone.utc)

    job_name = f"shift_opening_check_{chat_id}_{thread_id or 0}"
    for old_job in app.job_queue.get_jobs_by_name(job_name):
        old_job.schedule_removal()

    app.job_queue.run_daily(
        check_shift_opening_deadline,
        time=deadline_time,
        data={"chat_id": chat_id, "location": location, "thread_id": thread_id},
        name=job_name,
    )
    logger.info(
        f"Запланирована проверка открытия смены: {location} (chat={chat_id}, "
        f"thread={thread_id}) в {deadline_time} UTC"
    )


def schedule_all_printers_checks(app) -> None:
    """При старте бота — планируем проверки для всех известных точек/тем Shifts."""
    places = load_known_places()
    for key, info in places.items():
        if info.get("topic_type") != "shifts":
            continue
        location = info.get("location")
        if location not in SHIFT_START_HOURS:
            continue
        schedule_printers_check(
            app,
            chat_id=info["chat_id"],
            location=location,
            thread_id=info.get("thread_id"),
        )


def collect_sets_from_events(location: str, target_date: dt.date | None = None, chat_id: int | None = None) -> list[int]:
    """Собирает все сеты из событий за указанную смену.
    Окно: 20:00 Dubai (target_date) → 10:00 Dubai (target_date+1).
    Если chat_id передан — используем его. Иначе ищем через known_places."""
    if target_date is None:
        target_date = dt.date.today()

    if chat_id is not None:
        target_chat_ids = {chat_id}
    else:
        places = load_known_places()
        target_chat_ids = set()
        for info in places.values():
            if info.get("location") == location and info.get("topic_type") == "shifts":
                target_chat_ids.add(info["chat_id"])

    if not target_chat_ids:
        return []

    events = load_events()
    day_start = dt.datetime.combine(target_date, dt.time(16, 0), dt.timezone.utc)
    day_end = day_start + dt.timedelta(hours=14)

    sets_collected = []
    for cid in target_chat_ids:
        for e in events.get(str(cid), []):
            if e.get("type") != "set":
                continue
            try:
                t = dt.datetime.fromisoformat(e["time"])
            except Exception:
                continue
            if not (day_start <= t <= day_end):
                continue
            caption = e.get("caption") or ""
            m = re.search(r"size=(\d+)", caption)
            if m:
                sets_collected.append(int(m.group(1)))

    return sets_collected


# ──────────────────────────────────────────────
# АВТОСБОР SALES BREAKDOWN ИЗ ЧАТА SHIFTS
# ──────────────────────────────────────────────

def is_sale_line(text: str) -> bool:
    """Распознаёт строку продажи (содержит сумму AED + иконку оплаты)."""
    if not text:
        return False
    has_aed = bool(re.search(r"\d+\s*aed", text, re.IGNORECASE))
    has_icon = "💳" in text or "💵" in text
    return has_aed and has_icon


def parse_sale_line(text: str) -> dict | None:
    """Парсит строку Sales Breakdown.
    Возвращает dict с полями amount, payment ('card'/'cash'), is_tip, materials, phone, guest, photographer."""
    if not text:
        return None

    # Сумма AED
    m = re.search(r"(\d+)\s*AED", text, re.IGNORECASE)
    if not m:
        return None
    amount = int(m.group(1))

    # Тип оплаты
    if "💳" in text:
        payment = "card"
    elif "💵" in text:
        payment = "cash"
    else:
        return None

    # Это чаевые?
    is_tip = bool(re.search(r"\btip\b|чаев", text, re.IGNORECASE))

    # Материалы: считаем по подсказкам
    materials = {
        "w_f": 0,        # without frame (w/f)
        "frame": 0,
        "envelope": 0,
        "bag": 0,
        "business_card": 0,
        "bc_envelope": 0,
        "digital": 0,
    }
    # w/f → сколько штук без рамки
    for n in re.findall(r"(\d+)\s*w/f", text, re.IGNORECASE):
        materials["w_f"] += int(n)
    # Frame (не Frames в Bus.Card)
    for n in re.findall(r"(\d+)\s*Frame(?!s?\s*by)", text, re.IGNORECASE):
        materials["frame"] += int(n)
    # Bag(s)
    for n in re.findall(r"(\d+)\s*Bag", text, re.IGNORECASE):
        materials["bag"] += int(n)
    # Business Card with Envelope — визитка + её собственный маленький конверт
    bc_count = 0
    for n in re.findall(r"(\d+)\s*Business\s*Card", text, re.IGNORECASE):
        bc_count += int(n)
    materials["business_card"] = bc_count
    # Конверт для визитки появляется только когда явно написано "with Envelope"
    if re.search(r"Business\s*Card\s*with\s*Envelope", text, re.IGNORECASE):
        materials["bc_envelope"] = bc_count

    # A4 Envelope для фото — считаем "Envelope" минус "with Envelope" от визитки
    # Сначала удаляем "with Envelope" из текста, потом ищем оставшиеся Envelope
    text_no_bc_env = re.sub(r"Business\s*Card\s*with\s*Envelope", "", text, flags=re.IGNORECASE)
    for n in re.findall(r"(\d+)\s*Envelope", text_no_bc_env, re.IGNORECASE):
        materials["envelope"] += int(n)

    # Digital
    for n in re.findall(r"(\d+)\s*Digital", text, re.IGNORECASE):
        materials["digital"] += int(n)

    # Телефон
    phone_m = re.search(r"\+\d[\d\s\-]{6,}", text)
    phone = phone_m.group(0).strip() if phone_m else None

    # Фотограф в скобках в конце
    photog_m = re.search(r"\(([^)]+)\)\s*$", text)
    photographer = photog_m.group(1).strip() if photog_m else None

    # Гость — что-то между телефоном и скобкой
    guest = None
    if phone:
        after_phone = text[text.rfind(phone) + len(phone):]
        # убираем разделители, скобку
        after = re.sub(r"\(.*?\)", "", after_phone)
        after = after.strip(" —-\t")
        if after:
            guest = after

    return {
        "amount": amount,
        "payment": payment,
        "is_tip": is_tip,
        "materials": materials,
        "phone": phone,
        "guest": guest,
        "photographer": photographer,
        "raw": text.strip(),
    }


def split_sales_breakdown(text: str) -> list[str]:
    """Разбивает большой блок Sales Breakdown на отдельные строки-продажи.
    Каждая строка-продажа содержит AED + иконку (💳 или 💵)."""
    lines = []
    current = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if current:
                lines.append(current)
                current = None
            continue
        # Это начало новой строки продажи?
        # Признаки: содержит "AED" и иконку
        if re.search(r"\d+\s*AED", line, re.IGNORECASE) and ("💳" in line or "💵" in line):
            if current:
                lines.append(current)
            current = line
        elif current:
            # Продолжение предыдущей строки (перенос)
            current += " " + line
    if current:
        lines.append(current)
    return lines


def collect_sales_from_events(chat_id_shifts: int, target_date: dt.date | None = None) -> list[dict]:
    """Собирает все парсенные продажи из событий за день в Shifts-чате.
    Разбивает многострочные сообщения на отдельные продажи."""
    if target_date is None:
        target_date = dt.date.today()

    events = load_events().get(str(chat_id_shifts), [])
    # Окно ночной смены: 20:00 Dubai (target_date) → 10:00 Dubai (target_date+1)
    # = 16:00 UTC target_date → 06:00 UTC (target_date+1)
    day_start = dt.datetime.combine(target_date, dt.time(16, 0), dt.timezone.utc)
    day_end = day_start + dt.timedelta(hours=14)

    sales = []
    for e in events:
        if e.get("type") != "sale_line":
            continue
        try:
            t = dt.datetime.fromisoformat(e["time"])
        except Exception:
            continue
        if not (day_start <= t <= day_end):
            continue
        raw = e.get("caption") or ""
        # Разбиваем многострочный текст на отдельные продажи
        per_line = split_sales_breakdown(raw)
        if not per_line:
            # Может быть одна строка без переноса
            per_line = [raw]
        for line in per_line:
            parsed = parse_sale_line(line)
            if parsed:
                parsed["author"] = e.get("author")
                sales.append(parsed)

    return sales


def smart_shift_date() -> dt.date:
    """Возвращает дату смены. Если сейчас раннее утро Дубая (до 12:00) —
    это закрытие ночной смены, дата смены = вчера. Иначе сегодня."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    now_dubai = now_utc + dt.timedelta(hours=4)
    if now_dubai.hour < 12:
        return (now_dubai - dt.timedelta(days=1)).date()
    return now_dubai.date()


def find_shifts_chat_for_location(location: str) -> int | None:
    """Ищет chat_id Shifts-темы для указанной точки."""
    places = load_known_places()
    for info in places.values():
        if info.get("location") == location and info.get("topic_type") == "shifts":
            return info["chat_id"]
    return None


def calc_totals_from_sales(sales: list[dict]) -> dict:
    """Считает Total / Card / Cash / Tip по списку продаж."""
    card = 0
    cash = 0
    tip = 0
    for s in sales:
        if s["is_tip"]:
            tip += s["amount"]
            continue
        if s["payment"] == "card":
            card += s["amount"]
        elif s["payment"] == "cash":
            cash += s["amount"]
    return {
        "total": card + cash,
        "card": card,
        "cash": cash,
        "tip": tip,
    }


def collect_photographers_from_sales(sales: list[dict]) -> dict:
    """Группирует продажи по фотографу. Возвращает {имя: сумма}."""
    by_photog = {}
    for s in sales:
        if s["is_tip"]:
            continue
        photog = s.get("photographer")
        if not photog:
            continue
        # Нормализуем имя
        emp = find_employee_by_name(photog)
        name = emp["name"] if emp else photog
        by_photog[name] = by_photog.get(name, 0) + s["amount"]
    return by_photog


def calc_used_materials(sales: list[dict], defective: dict | None = None) -> dict:
    """Считает использованные расходники из продаж + defective."""
    used = {
        "paper": 0,
        "envelopes": 0,
        "frame": 0,
        "black_bags": 0,
        "business_cards": 0,
        "bc_envelopes": 0,
    }
    for s in sales:
        if s["is_tip"]:
            continue
        m = s["materials"]
        # Бумага = все распечатанные фото (w_f + frame)
        printed = m["w_f"] + m["frame"]
        used["paper"] += printed
        # Конверты для w/f
        used["envelopes"] += m["envelope"]
        # Рамки
        used["frame"] += m["frame"]
        # Чёрные пакеты — идут с рамками
        used["black_bags"] += m["bag"]
        # Визитки
        used["business_cards"] += m["business_card"]
        # Конверты для визиток
        used["bc_envelopes"] += m["bc_envelope"]

    # Добавляем defective
    if defective:
        used["paper"] += defective.get("prints", 0)
        used["envelopes"] += defective.get("envelopes", 0)
        used["frame"] += defective.get("frames", 0)
        used["black_bags"] += defective.get("bags", 0)

    return used


def calc_remaining_stock(location: str, sales: list[dict], defective: dict | None) -> dict | None:
    """Рассчитывает текущий остаток: previous - used. Если нет previous — None."""
    if not REMAINING_STOCK_FILE.exists():
        return None
    try:
        data = json.loads(REMAINING_STOCK_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    prev = data.get(location, {}).get("stock")
    if not prev:
        return None

    used = calc_used_materials(sales, defective)
    new_stock = {}
    for key in ["paper", "envelopes", "frame", "black_bags", "business_cards", "bc_envelopes"]:
        new_stock[key] = max(0, prev.get(key, 0) - used.get(key, 0))
    return new_stock


async def cmd_whereami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /whereami — отладочная. Показывает что Марса знает про текущее место."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    location, topic_type = recognize_place(update)

    lines = ["📍 Где я нахожусь:"]
    lines.append(f"• Group: {chat.title or '(без названия)'}")
    lines.append(f"• Распознал точку: **{location or 'не распознал'}**")
    lines.append(f"• Распознал тему: **{topic_type or 'не распознал (или основной чат)'}**")

    # Если это Shifts-тема известной точки — запланировать проверку принтеров
    if topic_type == "shifts" and location in SHIFT_START_HOURS:
        try:
            schedule_printers_check(
                context.application,
                chat_id=chat.id,
                location=location,
                thread_id=msg.message_thread_id if msg.is_topic_message else None,
            )
            start_h = SHIFT_START_HOURS[location]
            lines.append(f"• Проверка принтеров: ежедневно в {start_h:02d}:{PRINTERS_DEADLINE_MINUTES:02d}")
        except Exception:
            logger.exception("Не смог запланировать проверку")

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

# ──────────────────────────────────────────────
# ШАБЛОН END OF SHIFT REPORT — диалог /report в личке
# ──────────────────────────────────────────────
# Состояния диалога
(
    R_LOCATION,
    R_REVENUE,
    R_TIP,
    R_CASH_HOLDING,
    R_PHOTOGRAPHERS,
    R_SALES_BREAKDOWN,
    R_PHOTOSHOOT_SETS,
    R_EXPENSES,
    R_DEFECTIVE,
    R_REMAINING,
    R_LINKS,
    R_CONFIRM,
) = range(12)


def get_employee_info(username: str | None) -> dict | None:
    """По username возвращает полную запись из EMPLOYEES или None."""
    if not username:
        return None
    u = username.lstrip("@")
    return EMPLOYEES.get(u)


def find_cash_report_chat(location: str) -> dict | None:
    """Ищет в known_places.json запись Cash Report для указанной точки.
    Возвращает {chat_id, thread_id} или None."""
    places = load_known_places()
    for key, info in places.items():
        if info.get("location") == location and info.get("topic_type") == "cash_report":
            return {
                "chat_id": info["chat_id"],
                "thread_id": info.get("thread_id"),
            }
    return None


def parse_revenue_line(text: str) -> tuple[int, int, int] | None:
    """Парсит строку "2300 2000 300" → (total, card, cash). Допускает / и запятые."""
    cleaned = text.replace("/", " ").replace(",", " ").replace("AED", "").strip()
    parts = [p for p in cleaned.split() if p]
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            return None
    if len(nums) != 3:
        return None
    total, card, cash = nums
    if card + cash != total:
        return None
    return (total, card, cash)


def parse_photographers_line(text: str) -> list[dict] | None:
    """Парсит "Jennet 1500, Polina Kostyn 800" → [{name, sales, rate, salary}]."""
    parts = re.split(r"[,;\n]", text)
    result = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Берём последнее число в строке как продажи, остальное — имя
        m = re.match(r"^(.+?)\s+(\d+)\s*(?:AED)?$", part, re.IGNORECASE)
        if not m:
            return None
        name = m.group(1).strip()
        try:
            sales = int(m.group(2))
        except ValueError:
            return None
        # Ищем сотрудника по имени или алиасу
        emp = find_employee_by_name(name)
        rate = emp.get("rate") if emp else None
        canonical_name = emp.get("name") if emp else name  # каноническое имя из базы
        salary = int(round(sales * rate)) if rate else None
        result.append({"name": canonical_name, "sales": sales, "rate": rate, "salary": salary})
    return result if result else None


def format_final_report(data: dict) -> str:
    """Собирает финальный End of Shift Report БЕЗ пустых полей."""
    lines = []
    lines.append(f"📩 End of Shift Report {data['location']}")
    lines.append(f"Date: {data['date'].strftime('%d/%m/%Y')}")
    lines.append("")
    lines.append(f"💰 Total Revenue: {data['total']} AED")
    lines.append(f"Card: {data['card']} AED")
    lines.append(f"Cash: {data['cash']} AED")
    if data.get("tip"):
        lines.append(f"Tip: {data['tip']}")
    lines.append("")

    # Cash Holding
    if data.get("cash_holding"):
        lines.append("💵 Cash Holding:")
        # cash_holding может быть в виде "Anas 200, Mahir 100"
        holders = [h.strip() for h in re.split(r"[,;\n]", data["cash_holding"]) if h.strip()]
        for h in holders:
            m = re.match(r"^(.+?)\s+(\d+)$", h)
            if m:
                lines.append(f"Cash With {m.group(1).strip()}: {m.group(2)} AED")
            else:
                lines.append(f"Cash With {h}")
        lines.append("")

    photographers = data.get("photographers", [])

    # Individual Sales — только если 2+ фотографа
    if len(photographers) >= 2:
        lines.append("👥 Individual Sales:")
        for p in photographers:
            lines.append(f"Photographer {p['name']}: {p['sales']} AED")
        lines.append("")

    # Salaries — авторасчёт (всегда если есть фотографы)
    salaried = [p for p in photographers if p.get("salary") is not None]
    if salaried:
        lines.append("💼 Salaries:")
        for p in salaried:
            lines.append(f"Photographer {p['name']}: {p['salary']} AED")
        lines.append("")

    # Expenses (Taxi)
    if data.get("expenses"):
        lines.append("💸 Expenses:")
        # ожидаем формат "photographer 50, retoucher 40"
        parts = [p.strip() for p in re.split(r"[,;\n]", data["expenses"]) if p.strip()]
        for part in parts:
            m = re.match(r"^(photographer|retoucher)\s+(\d+)$", part.lower())
            if m:
                role = "Photographer" if m.group(1) == "photographer" else "Retoucher"
                lines.append(f"• {role} Taxi: {m.group(2)} AED")
            else:
                lines.append(f"• {part}")
        lines.append("")

    # Defective Materials
    if data.get("defective"):
        d = data["defective"]
        lines.append("🧾 Defective Materials:")
        lines.append(f"• A4 Prints: {d['prints']} pcs")
        lines.append(f"• A4 Envelopes: {d['envelopes']} pcs")
        lines.append(f"• A4 Frames: {d['frames']} pcs")
        lines.append(f"• A4 Bags: {d['bags']} pcs")
        lines.append("")

    # Remaining Consumables
    if data.get("remaining"):
        r = data["remaining"]
        lines.append("📦 Remaining Consumables:")
        lines.append(f"• A4 Paper: {r['paper']} pcs")
        lines.append(f"• A4 Envelopes: {r['envelopes']} pcs")
        lines.append(f"• A4 Frame: {r['frame']} pcs")
        lines.append(f"• A4 Black Bags: {r['black_bags']} pcs")
        lines.append(f"• Business Cards: {r['business_cards']} pcs")
        lines.append(f"• Business Card Envelopes: {r['bc_envelopes']} pcs")
        lines.append("")

    # Sales Breakdown
    if data.get("sales_breakdown"):
        lines.append("💰 Sales Breakdown:")
        lines.append(data["sales_breakdown"].strip())
        lines.append("")

    # Photoshoot Sets
    if data.get("photoshoot_sets"):
        lines.append("📸 Photoshoot Sets:")
        lines.append(data["photoshoot_sets"].strip())
        lines.append("")

    # Links
    if data.get("links"):
        lines.append("🔗 Links:")
        lines.append(data["links"].strip())

    return "\n".join(lines).strip()


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Старт диалога /report — только в личке, только для авторизованных."""
    chat = update.effective_chat
    if not chat or chat.type != "private":
        await update.effective_message.reply_text(
            "Эту команду используй в личке со мной — там пошагово соберём отчёт."
        )
        return ConversationHandler.END

    user = update.effective_user
    emp = get_employee_info(user.username if user else None)

    # Приватность: только сотрудники из EMPLOYEES имеют доступ
    if not emp:
        if user:
            log_unauthorized_access(user)
        await update.effective_message.reply_text(
            "У тебя нет доступа к этой команде.\n"
            "Если ты в команде Marsa Moments — обратись к Сабине, чтобы она тебя добавила."
        )
        return ConversationHandler.END

    name = emp["name"]
    context.user_data["report"] = {
        "name": name,
        "username": user.username if user else None,
        "date": dt.date.today(),
    }

    greeting = random.choice(GREETINGS_REPORT_START).format(name=name)
    await update.effective_message.reply_text(
        f"{greeting}\n"
        f"На какой точке сегодня была? Напиши название (например: Avenue).\n\n"
        f"Если передумала — /cancel"
    )
    return R_LOCATION


async def on_location_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработка текстового ввода точки."""
    text = (update.effective_message.text or "").strip()

    # Используем существующий распознаватель из knowledge.py
    location = detect_location(text)

    if not location:
        await update.effective_message.reply_text(
            "Не понял точку. Напиши одно из названий, которые ты сама используешь "
            "в чатах (например: Avenue, или O Lounge, или Chayka...).\n\n"
            "Если передумала — /cancel"
        )
        return R_LOCATION

    context.user_data["report"]["location"] = location

    await update.effective_message.reply_text(
        f"Точка: *{location}*\n\n"
        f"Кидай выручку одной строкой: `Total Card Cash`\n"
        f"Например: `2300 2000 300`",
        parse_mode="Markdown",
    )
    return R_REVENUE


async def on_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Парсит выручку."""
    text = update.effective_message.text or ""
    parsed = parse_revenue_line(text)
    if not parsed:
        await update.effective_message.reply_text(
            "Что-то не так. Мне нужна общая сумма, потом сколько оплатили картой, потом наличными.\n\n"
            "Пример: `2300 2000 300` (Total 2300 = Card 2000 + Cash 300)",
            parse_mode="Markdown",
        )
        return R_REVENUE

    total, card, cash = parsed
    context.user_data["report"]["total"] = total
    context.user_data["report"]["card"] = card
    context.user_data["report"]["cash"] = cash

    # Имя текущего пользователя для примера чаевых
    name_for_example = context.user_data["report"].get("name", "Anas")

    await update.effective_message.reply_text(
        f"Записал. Касса {total} / Card {card} / Cash {cash}\n\n"
        f"Чаевые были? Кидай сумму и кому (например: `100 {name_for_example}` — 100 AED для {name_for_example}).\n"
        f"Если нет — /skip",
        parse_mode="Markdown",
    )
    return R_TIP


async def on_tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Чаевые — опциональные. Дальше Cash Holding если cash > 0."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["report"]["tip"] = text

    cash = context.user_data["report"].get("cash", 0)
    if cash > 0:
        await update.effective_message.reply_text(
            f"У кого остались наличные ({cash} AED)?\n"
            f"Формат: `Anas 200, Mahir 100`\n"
            f"Если всё в одних руках — `Anas {cash}`",
            parse_mode="Markdown",
        )
        return R_CASH_HOLDING

    # Наличных нет — пропускаем Cash Holding
    return await ask_photographers(update, context)


async def on_cash_holding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cash Holding — у кого наличные."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["report"]["cash_holding"] = text
    return await ask_photographers(update, context)


async def ask_photographers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Спросить кто работал и сколько каждый сделал."""
    quip = random.choice(SALARY_QUIPS)
    await update.effective_message.reply_text(
        "Кто работал и сколько каждый сделал?\n"
        "Формат: `Имя сумма, Имя сумма`\n"
        "Например: `Jennet 1500, Polina Kostyn 800`\n\n"
        f"{quip}",
        parse_mode="Markdown",
    )
    return R_PHOTOGRAPHERS


async def on_photographers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Фотографы и продажи."""
    text = update.effective_message.text or ""
    parsed = parse_photographers_line(text)
    if not parsed:
        await update.effective_message.reply_text(
            "Не понял. Формат: `Имя сумма, Имя сумма`\n"
            "Пример: `Jennet 1500, Polina Kostyn 800`",
            parse_mode="Markdown",
        )
        return R_PHOTOGRAPHERS

    # Проверка: сумма по фотографам = total (без чаевых)
    total_photographers = sum(p["sales"] for p in parsed)
    expected = context.user_data["report"]["total"]
    if total_photographers != expected:
        await update.effective_message.reply_text(
            f"⚠️ Сумма по фотографам = {total_photographers}, а Total Revenue = {expected}. "
            f"Не сходится — проверь и пришли ещё раз."
        )
        return R_PHOTOGRAPHERS

    context.user_data["report"]["photographers"] = parsed

    # Покажем расчёт зарплат
    salary_lines = []
    for p in parsed:
        if p.get("salary") is not None:
            salary_lines.append(f"  • {p['name']}: {p['salary']} AED ({int(p['rate']*100)}%)")
        else:
            salary_lines.append(f"  • {p['name']}: ставка не нашёл, посчитай зарплату вручную")

    salaries_block = "\n".join(salary_lines)

    await update.effective_message.reply_text(
        f"Окей, зарплаты разложил 💼\n{salaries_block}\n\n"
        f"💰 Теперь кидай Sales Breakdown одним сообщением — как обычно постишь.\n"
        f"⚠️ Tip сюда не пиши — он уже отдельно зафиксирован.\n\n"
        f"Например:\n"
        f"1. 300 AED 💳 — 1 Frame, 1 Bag, 1 Business Card with Envelope — +971... — Имя гостя\n"
        f"2. 500 AED 💵 — 2 w/f, 2 Envelope, 2 Business Card with Envelope — +971... — Имя\n\n"
        f"Или /skip если строк нет."
    )
    return R_SALES_BREAKDOWN


async def on_sales_breakdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sales Breakdown свободным текстом."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        # Если упомянут Tip — предупредить
        if re.search(r"\btip\b|\bчаев", text, re.IGNORECASE):
            await update.effective_message.reply_text(
                "Вижу строку про Tip в Sales Breakdown — убери её, пожалуйста. "
                "Tip уже зафиксирован отдельно, иначе будет удвоение в отчёте.\n\n"
                "Пришли Sales Breakdown заново — только продажи фото."
            )
            return R_SALES_BREAKDOWN
        context.user_data["report"]["sales_breakdown"] = text

    # АВТОСБОР СЕТОВ из чата за сегодня
    location = context.user_data["report"].get("location")
    target_date = context.user_data["report"].get("date") or dt.date.today()
    sets_collected = collect_sets_from_events(location, target_date) if location else []

    if sets_collected:
        # Формируем блок Photoshoot Sets автоматически
        sets_lines = [f"Set {i+1}: {n}" for i, n in enumerate(sets_collected)]
        total_pics = sum(sets_collected)
        sets_text = "\n".join(sets_lines) + f"\nTotal Pics: {total_pics}"
        context.user_data["report"]["photoshoot_sets"] = sets_text
        context.user_data["report"]["sets_auto"] = True

        await update.effective_message.reply_text(
            f"Сеты собрал из чата за сегодня ({len(sets_collected)} шт, всего {total_pics} фото). "
            f"Шаг пропускаю.\n\n"
            f"Дальше — такси, было?\n"
            f"Формат: `photographer 50, retoucher 40`\n"
            f"Если такси не было — /skip",
            parse_mode="Markdown",
        )
        return R_EXPENSES

    # Если не нашёл сетов — спрашиваем как раньше
    await update.effective_message.reply_text(
        "Сеты не нашёл в чате. Кидай одним сообщением:\n"
        "Set 1: 11\n"
        "Set 2: 8\n"
        "Set 3: 12\n\n"
        "Или /skip."
    )
    return R_PHOTOSHOOT_SETS


async def on_photoshoot_sets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сеты свободным текстом. Дальше — Expenses (такси)."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["report"]["photoshoot_sets"] = text

    await update.effective_message.reply_text(
        "💸 Такси — было?\n"
        "Формат: `photographer 50, retoucher 40` (любое из двух можно пропустить)\n"
        "Если такси не было совсем — /skip",
        parse_mode="Markdown",
    )
    return R_EXPENSES


async def on_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Расходы на такси."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["report"]["expenses"] = text

    await update.effective_message.reply_text(
        "🧾 Defective Materials — что забраковали?\n"
        "Пришли в столбик, каждое поле с новой строки:\n\n"
        "```\n"
        "Prints: 58\n"
        "Envelopes: 0\n"
        "Frames: 1\n"
        "Bags: 0\n"
        "```\n\n"
        "Если ничего не забраковали — /skip",
        parse_mode="Markdown",
    )
    return R_DEFECTIVE


async def on_defective(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Defective Materials — формат `Label: число` по строкам."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        result = parse_labeled_lines(text, expected_labels=["prints", "envelopes", "frames", "bags"])
        if result is None:
            await update.effective_message.reply_text(
                "Не понял. Нужно 4 строки в формате `Label: число`:\n\n"
                "```\n"
                "Prints: 58\n"
                "Envelopes: 0\n"
                "Frames: 1\n"
                "Bags: 0\n"
                "```\n\n"
                "Или /skip",
                parse_mode="Markdown",
            )
            return R_DEFECTIVE
        context.user_data["report"]["defective"] = result

    await update.effective_message.reply_text(
        "📦 Remaining Consumables — сколько осталось?\n"
        "Пришли в столбик, каждое поле с новой строки:\n\n"
        "```\n"
        "Paper: 4770\n"
        "Envelopes: 280\n"
        "Frame: 19\n"
        "Black Bags: 54\n"
        "Business Cards: 83\n"
        "BC Envelopes: 764\n"
        "```\n\n"
        "Если не считали — /skip",
        parse_mode="Markdown",
    )
    return R_REMAINING


async def on_remaining(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Remaining Consumables — формат `Label: число` по строкам."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        result = parse_labeled_lines(
            text,
            expected_labels=["paper", "envelopes", "frame", "black bags", "business cards", "bc envelopes"],
        )
        if result is None:
            await update.effective_message.reply_text(
                "Не понял. Нужно 6 строк в формате `Label: число`:\n\n"
                "```\n"
                "Paper: 4770\n"
                "Envelopes: 280\n"
                "Frame: 19\n"
                "Black Bags: 54\n"
                "Business Cards: 83\n"
                "BC Envelopes: 764\n"
                "```\n\n"
                "Или /skip",
                parse_mode="Markdown",
            )
            return R_REMAINING
        context.user_data["report"]["remaining"] = result

        # Сохраняем как актуальные остатки для этой точки и проверяем пороги
        location = context.user_data["report"].get("location")
        if location:
            save_remaining_stock(location, result)
            low = detect_low_stock(result)
            if low:
                warning = format_low_stock_warning(location, low)
                # Предупреждение Sabin'е (она запускает /report)
                await update.effective_message.reply_text(warning)

    await update.effective_message.reply_text(
        "🔗 Кидай ссылки на отснятое — Pixieset / Drive (одним сообщением).\n"
        "Если их нет — /skip"
    )
    return R_LINKS


REMAINING_STOCK_FILE = Path("remaining_stock.json")

# Минимальные запасы — ниже = предупреждение
LOW_STOCK_THRESHOLDS = {
    "paper": 200,
    "envelopes": 50,
    "frame": 10,
    "black_bags": 20,
    "business_cards": 30,
    "bc_envelopes": 100,
}

STOCK_LABEL_NAMES = {
    "paper": "A4 Paper",
    "envelopes": "A4 Envelopes",
    "frame": "A4 Frames",
    "black_bags": "A4 Black Bags",
    "business_cards": "Business Cards",
    "bc_envelopes": "Business Card Envelopes",
}


def save_remaining_stock(location: str, stock: dict) -> None:
    """Сохраняет текущие остатки расходников по точке."""
    try:
        data = {}
        if REMAINING_STOCK_FILE.exists():
            try:
                data = json.loads(REMAINING_STOCK_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[location] = {
            "stock": stock,
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        REMAINING_STOCK_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не смог сохранить remaining_stock")


def detect_low_stock(stock: dict) -> list[tuple[str, int, int]]:
    """Возвращает список (key, value, threshold) для позиций ниже порога."""
    low = []
    for key, threshold in LOW_STOCK_THRESHOLDS.items():
        value = stock.get(key)
        if value is not None and value < threshold:
            low.append((key, value, threshold))
    return low


def format_low_stock_warning(location: str, low_items: list) -> str:
    """Формирует текст предупреждения о низких остатках."""
    lines = [f"⚠️ На {location} заканчиваются расходники:"]
    for key, value, threshold in low_items:
        label = STOCK_LABEL_NAMES.get(key, key)
        lines.append(f"• {label}: {value} (порог {threshold})")
    lines.append("\nНадо подвезти.")
    return "\n".join(lines)


def parse_labeled_lines(text: str, expected_labels: list[str]) -> dict | None:
    """Парсит ввод вида `Label: число` по строкам.
    Возвращает {normalized_label: int} или None если не нашлось всех ожидаемых.
    Сортирует лейблы по длине (длинные первыми), чтобы 'BC Envelopes' не матчилось как 'Envelopes'."""
    # Длинные лейблы — первыми, для приоритета при матчинге
    sorted_labels = sorted(expected_labels, key=len, reverse=True)

    result = {}
    used_labels = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-zА-Яа-я\s]+?)[:\s]+(\d+)\s*(?:pcs|шт)?\s*$", line, re.IGNORECASE)
        if not m:
            continue
        label = m.group(1).strip().lower()
        for expected in sorted_labels:
            if expected in used_labels:
                continue
            if label == expected.lower():
                key = expected.lower().replace(" ", "_")
                result[key] = int(m.group(2))
                used_labels.add(expected)
                break
    if len(result) != len(expected_labels):
        return None
    return result


async def on_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ссылки. Дальше показываем черновик и просим подтвердить текстом."""
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["report"]["links"] = text

    # Собираем черновик
    data = context.user_data["report"]
    draft = format_final_report(data)

    # Проверяем что Марса знает Cash Report этой точки
    target = find_cash_report_chat(data["location"])
    if target:
        publish_hint = f"✅ Опубликую в Cash Report *{data['location']}*."
    else:
        publish_hint = (
            f"⚠️ Пока не знаю где Cash Report *{data['location']}* — "
            f"пусть менеджер один раз зайдёт в эту тему и напишет `/whereami`. "
            f"Сейчас просто верну текст, скопируешь сама."
        )

    await update.effective_message.reply_text(
        f"Готово, вот отчёт:\n\n"
        f"```\n{draft}\n```\n\n"
        f"{publish_hint}\n\n"
        f"Публиковать? Напиши `да` чтобы опубликовать или `нет` чтобы отменить.",
        parse_mode="Markdown",
    )
    return R_CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение публикации — текстом 'да' или 'нет'."""
    text = (update.effective_message.text or "").strip().lower()

    if text in ("нет", "no", "n", "отмена", "cancel"):
        await update.effective_message.reply_text(
            "Окей, отбой. Когда будешь готов(а) — /report ещё раз 👌"
        )
        context.user_data.pop("report", None)
        return ConversationHandler.END

    if text not in ("да", "yes", "y", "+", "ок", "ok", "опубликовать"):
        await update.effective_message.reply_text(
            "Напиши `да` или `нет`."
        )
        return R_CONFIRM

    # Публикуем
    data = context.user_data["report"]
    final_text = format_final_report(data)
    target = find_cash_report_chat(data["location"])

    if target:
        try:
            await context.bot.send_message(
                chat_id=target["chat_id"],
                text=final_text,
                message_thread_id=target.get("thread_id"),
            )
            await update.effective_message.reply_text(
                f"Готово! Опубликовал в Cash Report {data['location']}. Хорошей ночи 🌙"
            )
        except Exception as e:
            logger.exception("Не смог опубликовать отчёт")
            await update.effective_message.reply_text(
                f"⚠️ Не получилось опубликовать ({e}).\n\nВот текст, скопируй сам(а):\n\n"
                f"```\n{final_text}\n```",
                parse_mode="Markdown",
            )
    else:
        await update.effective_message.reply_text(
            f"Пока не знаю Cash Report {data['location']}. Скопируй текст и постни сам(а):\n\n"
            f"```\n{final_text}\n```",
            parse_mode="Markdown",
        )

    context.user_data.pop("report", None)
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /cancel для выхода из диалога."""
    context.user_data.pop("report", None)
    context.user_data.pop("close", None)
    await update.effective_message.reply_text("Отменено.")
    return ConversationHandler.END


# ──────────────────────────────────────────────
# КОМАНДА /close — автосбор отчёта в Cash Report
# ──────────────────────────────────────────────

# Состояния диалога /close
(
    C_TIP,
    C_CASH_HOLDING,
    C_EXPENSES,
    C_DEFECTIVE,
    C_CONFIRM,
) = range(100, 105)  # отдельный диапазон чтобы не пересекаться с R_*


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запуск автосбора отчёта в Cash Report."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return ConversationHandler.END

    # Только в группах
    if chat.type == "private":
        await msg.reply_text(
            "Эту команду используй в теме Cash Report твоей точки. "
            "В личке используй /report."
        )
        return ConversationHandler.END

    # Только для авторизованных
    emp = get_employee_info(user.username)
    if not emp:
        if user:
            log_unauthorized_access(user)
        await msg.reply_text(
            "У тебя нет доступа к этой команде. "
            "Если ты в команде Marsa Moments — обратись к Сабине."
        )
        return ConversationHandler.END

    # Определяем точку и тип темы
    location, topic_type = recognize_place(update)
    if topic_type != "cash_report":
        await msg.reply_text(
            "Эту команду нужно запускать в теме *Cash Report* твоей точки.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if not location:
        await msg.reply_text(
            "Не понял какая это точка. Напиши /whereami здесь и в Shifts-теме."
        )
        return ConversationHandler.END

    # В Telegram групповой чат и его темы (Shifts, Cash Report) имеют ОДИН chat_id,
    # отличаются только thread_id. Так что все события группы (включая sale_line
    # из Shifts темы) сохранены под этим же chat_id. Не зависим от known_places.
    shifts_chat_id = chat.id

    # СОБИРАЕМ ДАННЫЕ ЗА СМЕНУ (с учётом ночной смены)
    target_date = smart_shift_date()
    sales = collect_sales_from_events(shifts_chat_id, target_date)
    sets_collected = collect_sets_from_events(location, target_date, chat_id=shifts_chat_id)
    totals = calc_totals_from_sales(sales)
    photographers = collect_photographers_from_sales(sales)

    name = emp["name"]
    context.user_data["close"] = {
        "name": name,
        "username": user.username,
        "user_id": user.id,
        "chat_id": chat.id,
        "thread_id": msg.message_thread_id if msg.is_topic_message else None,
        "location": location,
        "shifts_chat_id": shifts_chat_id,
        "date": target_date,
        "sales": sales,
        "sets": sets_collected,
        "totals": totals,
        "photographers": photographers,
    }

    # Показываем что собрали
    lines = [f"📊 Собрал данные за смену — {location}, {target_date.strftime('%d/%m/%Y')}:\n"]

    if totals["total"] > 0:
        lines.append(f"💰 Total: {totals['total']} AED (Card {totals['card']} / Cash {totals['cash']})")
        if totals["tip"]:
            lines.append(f"💸 Tip: {totals['tip']} AED")
    else:
        lines.append("⚠️ Продаж в чате не нашёл за сегодня.")

    if sets_collected:
        lines.append(f"📸 Сеты: {len(sets_collected)} шт ({sum(sets_collected)} фото всего)")
    else:
        lines.append("⚠️ Сеты не собрал — никто не постил.")

    if photographers:
        ph_str = ", ".join(f"{n}: {s} AED" for n, s in photographers.items())
        lines.append(f"👥 Работали: {ph_str}")

    lines.append("")
    lines.append("Дальше уточню что не знаю. Можно /cancel.")
    await msg.reply_text("\n".join(lines))

    # Спрашиваем чаевые если не нашёл
    if totals["tip"] == 0:
        await msg.reply_text(
            f"💸 Чаевые были? Кидай сумму и кому (например: `100 {name}`).\n"
            f"Если нет — /skip",
            parse_mode="Markdown",
        )
        return C_TIP
    else:
        # Tip уже знаем — идём к Cash Holding
        return await close_step_cash_holding(update, context)


async def on_close_tip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await maybe_handle_verdict_reply(update, context):
        return C_TIP
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["close"]["tip_manual"] = text
    return await close_step_cash_holding(update, context)


async def close_step_cash_holding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data["close"]
    cash = data["totals"]["cash"]
    if cash > 0:
        await update.effective_message.reply_text(
            f"💵 У кого остались наличные ({cash} AED)?\n"
            f"Формат: `Anas 200, Mahir 100`. Если всё в одних руках — `Anas {cash}`",
            parse_mode="Markdown",
        )
        return C_CASH_HOLDING
    return await close_step_expenses(update, context)


async def on_close_cash_holding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await maybe_handle_verdict_reply(update, context):
        return C_CASH_HOLDING
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        if not re.search(r"\d", text):
            await update.effective_message.reply_text(
                "Не понял. Нужно имя и сумма, например `Anas 200` или `Anas 100, Mahir 200`.\n"
                "Если наличных ни у кого нет — /skip"
            )
            return C_CASH_HOLDING
        context.user_data["close"]["cash_holding"] = text
    return await close_step_expenses(update, context)


async def close_step_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "💸 Такси было?\n"
        "Формат: `photographer 50, retoucher 40`. Если не было — /skip",
        parse_mode="Markdown",
    )
    return C_EXPENSES


async def on_close_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await maybe_handle_verdict_reply(update, context):
        return C_EXPENSES
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        context.user_data["close"]["expenses"] = text
    return await close_step_defective(update, context)


async def close_step_defective(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text(
        "🧾 Defective Materials — что забраковали?\n"
        "Пришли в столбик:\n\n"
        "```\n"
        "Prints: 58\n"
        "Envelopes: 0\n"
        "Frames: 1\n"
        "Bags: 0\n"
        "```\n\n"
        "Если ничего — /skip",
        parse_mode="Markdown",
    )
    return C_DEFECTIVE


async def on_close_defective(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await maybe_handle_verdict_reply(update, context):
        return C_DEFECTIVE
    text = (update.effective_message.text or "").strip()
    if text and text != "/skip":
        result = parse_labeled_lines(text, expected_labels=["prints", "envelopes", "frames", "bags"])
        if result is None:
            await update.effective_message.reply_text(
                "Не понял. Нужно 4 строки `Label: число` или /skip"
            )
            return C_DEFECTIVE
        context.user_data["close"]["defective"] = result

    # Рассчитываем Remaining автоматически
    data = context.user_data["close"]
    remaining = calc_remaining_stock(data["location"], data["sales"], data.get("defective"))
    data["remaining"] = remaining

    # Показываем черновик финального отчёта
    draft = format_close_report(data)
    await update.effective_message.reply_text(
        f"Готовый отчёт:\n\n```\n{draft}\n```\n\n"
        f"Публиковать? Напиши `да` или `нет`.",
        parse_mode="Markdown",
    )
    return C_CONFIRM


async def on_close_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await maybe_handle_verdict_reply(update, context):
        return C_CONFIRM
    text = (update.effective_message.text or "").strip().lower()
    if text in ("нет", "no", "n", "отмена"):
        await update.effective_message.reply_text("Окей, отбой. Когда будешь готов(а) — /close ещё раз.")
        context.user_data.pop("close", None)
        return ConversationHandler.END

    if text not in ("да", "yes", "y", "+", "ок", "ok", "опубликовать"):
        await update.effective_message.reply_text("Напиши `да` или `нет`.")
        return C_CONFIRM

    data = context.user_data["close"]
    final_text = format_close_report(data)

    # Публикуем В ТУ ЖЕ ТЕМУ откуда запустили
    try:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=final_text,
            message_thread_id=data.get("thread_id"),
        )
    except Exception as e:
        await update.effective_message.reply_text(
            f"⚠️ Не получилось опубликовать ({e}).\nВот текст:\n\n```\n{final_text}\n```",
            parse_mode="Markdown",
        )
        context.user_data.pop("close", None)
        return ConversationHandler.END

    # Сохраняем новый остаток
    if data.get("remaining"):
        save_remaining_stock(data["location"], data["remaining"])

    await update.effective_message.reply_text("Опубликовал. Хорошей ночи 🌙")
    context.user_data.pop("close", None)
    return ConversationHandler.END


def format_close_report(data: dict) -> str:
    """Формирует End of Shift Report из собранных данных."""
    lines = []
    lines.append(f"📩 End of Shift Report {data['location']}")
    lines.append(f"Date: {data['date'].strftime('%d/%m/%Y')}")
    lines.append("")

    t = data["totals"]
    lines.append(f"💰 Total Revenue: {t['total']} AED")
    lines.append(f"Card: {t['card']} AED")
    lines.append(f"Cash: {t['cash']} AED")
    tip_value = t["tip"] if t["tip"] else data.get("tip_manual")
    if tip_value:
        lines.append(f"Tip: {tip_value}")
    lines.append("")

    # Cash Holding
    if data.get("cash_holding"):
        lines.append("💵 Cash Holding:")
        for h in re.split(r"[,;\n]", data["cash_holding"]):
            h = h.strip()
            if not h:
                continue
            m = re.match(r"^(.+?)\s+(\d+)$", h)
            if m:
                lines.append(f"Cash With {m.group(1).strip()}: {m.group(2)} AED")
            else:
                lines.append(f"Cash With {h}")
        lines.append("")

    # Individual Sales и Salaries
    photogs = data.get("photographers", {})
    if len(photogs) >= 2:
        lines.append("👥 Individual Sales:")
        for n, s in photogs.items():
            lines.append(f"Photographer {n}: {s} AED")
        lines.append("")
    if photogs:
        lines.append("💼 Salaries:")
        for n, s in photogs.items():
            emp = find_employee_by_name(n)
            rate = emp.get("rate") if emp else None
            if rate:
                lines.append(f"Photographer {n}: {int(round(s * rate))} AED")
        lines.append("")

    # Expenses
    if data.get("expenses"):
        lines.append("💸 Expenses:")
        for part in re.split(r"[,;\n]", data["expenses"]):
            part = part.strip().lower()
            if not part:
                continue
            m = re.match(r"^(photographer|retoucher)\s+(\d+)$", part)
            if m:
                role = "Photographer" if m.group(1) == "photographer" else "Retoucher"
                lines.append(f"• {role} Taxi: {m.group(2)} AED")
        lines.append("")

    # Defective Materials
    if data.get("defective"):
        d = data["defective"]
        lines.append("🧾 Defective Materials:")
        lines.append(f"• A4 Prints: {d['prints']} pcs")
        lines.append(f"• A4 Envelopes: {d['envelopes']} pcs")
        lines.append(f"• A4 Frames: {d['frames']} pcs")
        lines.append(f"• A4 Bags: {d['bags']} pcs")
        lines.append("")

    # Remaining (расчётный)
    if data.get("remaining"):
        r = data["remaining"]
        lines.append("📦 Remaining Consumables:")
        lines.append(f"• A4 Paper: {r['paper']} pcs")
        lines.append(f"• A4 Envelopes: {r['envelopes']} pcs")
        lines.append(f"• A4 Frame: {r['frame']} pcs")
        lines.append(f"• A4 Black Bags: {r['black_bags']} pcs")
        lines.append(f"• Business Cards: {r['business_cards']} pcs")
        lines.append(f"• Business Card Envelopes: {r['bc_envelopes']} pcs")
        lines.append("")

    # Sales Breakdown — из собранных строк
    sales = data.get("sales", [])
    sales_no_tips = [s for s in sales if not s["is_tip"]]
    if sales_no_tips:
        lines.append("💰 Sales Breakdown:")
        for i, s in enumerate(sales_no_tips, 1):
            lines.append(f"{i}. {s['raw']}")
        lines.append("")

    # Photoshoot Sets
    sets = data.get("sets", [])
    if sets:
        lines.append("📸 Photoshoot Sets:")
        for i, n in enumerate(sets, 1):
            lines.append(f"Set {i}: {n}")
        lines.append(f"Total Pics: {sum(sets)}")

    return "\n".join(lines).strip()


async def cmd_close_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена /close — только тот кто запустил."""
    user = update.effective_user
    data = context.user_data.get("close")
    if not data or not user or data.get("user_id") != user.id:
        return ConversationHandler.END
    context.user_data.pop("close", None)
    await update.effective_message.reply_text("Отменено.")
    return ConversationHandler.END


def is_reply_to_verdict(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True если сообщение — reply на бот-сообщение с вердиктом/ошибками отчёта."""
    msg = update.effective_message
    if not msg or not msg.reply_to_message:
        return False
    if not msg.reply_to_message.from_user:
        return False
    if msg.reply_to_message.from_user.id != context.bot.id:
        return False
    replied_text = msg.reply_to_message.text or ""
    markers = [
        "Нашёл проблем", "Found", "Отчёт чистый", "Report is clean",
        "Sales Breakdown", "Lines", "В строках",
    ]
    return any(m in replied_text for m in markers)


async def maybe_handle_verdict_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Если это reply на старое бот-сообщение с вердиктом — обработать как объяснение.
    Возвращает True если обработано (нужно остаться в текущем шаге)."""
    if not is_reply_to_verdict(update, context):
        return False
    await handle_explanation_reply(update, context)
    return True


# ──────────────────────────────────────────────
# GM-ЧАТ — куда слать зарплаты и штрафы
# ──────────────────────────────────────────────

GM_CHAT_FILE = Path("gm_chat.json")


def get_gm_chat() -> dict | None:
    """Возвращает {chat_id, thread_id} или None."""
    if not GM_CHAT_FILE.exists():
        return None
    try:
        return json.loads(GM_CHAT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def set_gm_chat(chat_id: int, thread_id: int | None) -> None:
    GM_CHAT_FILE.write_text(
        json.dumps({"chat_id": chat_id, "thread_id": thread_id}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def send_to_gm(context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Отправить сообщение в GM-чат. Возвращает True если успех."""
    gm = get_gm_chat()
    if not gm:
        logger.warning("GM-чат не настроен (нет gm_chat.json). Сообщение НЕ отправлено: %s", text[:100])
        return False
    try:
        await context.bot.send_message(
            chat_id=gm["chat_id"],
            message_thread_id=gm.get("thread_id"),
            text=text,
        )
        return True
    except Exception:
        logger.exception("Не смог отправить в GM-чат")
        return False


async def cmd_setgm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Назначить текущий чат как GM-чат. Только Sabina."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return

    emp = get_employee_info(user.username)
    if not emp or emp.get("role") != "gm":
        await msg.reply_text("Только GM может назначить этот чат как GM-чат.")
        return

    thread_id = msg.message_thread_id if msg.is_topic_message else None
    set_gm_chat(chat.id, thread_id)
    await msg.reply_text(
        f"✅ Этот чат назначен как GM-чат.\n"
        f"Сюда буду слать зарплаты, штрафы за опоздания, тревоги и утреннюю сводку.\n\n"
        f"chat_id: `{chat.id}`\n"
        f"thread_id: `{thread_id}`",
        parse_mode="Markdown",
    )


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Возвращает user_id и chat_id — для отладки."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.effective_message
    if not chat or not user or not msg:
        return
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    await msg.reply_text(
        f"👤 user_id: `{user.id}`\n"
        f"📍 chat_id: `{chat.id}`\n"
        f"🧵 thread_id: `{thread_id}`",
        parse_mode="Markdown",
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие в личке с ботом — только для своих."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    if chat.type != "private":
        return

    emp = get_employee_info(user.username)
    name = emp["name"] if emp else (user.first_name or "там")

    if emp:
        greeting = random.choice(GREETINGS_START).format(name=name)
        await update.effective_message.reply_text(
            f"{greeting}\n\n"
            f"/report — собрать End of Shift пошагово\n"
            f"/cancel — отменить если что"
        )
    else:
        # Логируем попытку доступа
        log_unauthorized_access(user)
        await update.effective_message.reply_text(
            f"Привет, {name}. У тебя пока нет доступа к этому боту.\n"
            f"Если ты в команде Marsa Moments — обратись к Сабине, чтобы она тебя добавила."
        )


def log_unauthorized_access(user) -> None:
    """Записываем попытку доступа от неавторизованного пользователя."""
    try:
        path = Path("unauthorized_access.json")
        log = []
        if path.exists():
            try:
                log = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log = []
        entry = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
        }
        log.append(entry)
        # храним только последние 200 записей
        log = log[-200:]
        path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Не смог записать unauthorized_access.json")


# ──────────────────────────────────────────────

def main() -> None:
    if "СЮДА_" in TELEGRAM_TOKEN or "СЮДА_" in ANTHROPIC_API_KEY:
        raise SystemExit(
            "Сначала задай TELEGRAM_TOKEN и ANTHROPIC_API_KEY "
            "(переменные окружения или в коде)."
        )

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Диалог /report — только в личке с ботом
    report_conv = ConversationHandler(
        entry_points=[CommandHandler("report", cmd_report)],
        states={
            R_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_location_text)],
            R_REVENUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_revenue)],
            R_TIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_tip), CommandHandler("skip", on_tip)],
            R_CASH_HOLDING: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_cash_holding), CommandHandler("skip", on_cash_holding)],
            R_PHOTOGRAPHERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_photographers)],
            R_SALES_BREAKDOWN: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_sales_breakdown), CommandHandler("skip", on_sales_breakdown)],
            R_PHOTOSHOOT_SETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_photoshoot_sets), CommandHandler("skip", on_photoshoot_sets)],
            R_EXPENSES: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_expenses), CommandHandler("skip", on_expenses)],
            R_DEFECTIVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_defective), CommandHandler("skip", on_defective)],
            R_REMAINING: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_remaining), CommandHandler("skip", on_remaining)],
            R_LINKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_links), CommandHandler("skip", on_links)],
            R_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        per_chat=False,  # стейт привязан к user_id, не к чату
    )
    # Команды без диалогов регистрируем ПЕРВЫМИ — group=-1
    # чтобы они работали поверх любых активных ConversationHandler'ов
    app.add_handler(CommandHandler("start", cmd_start), group=-1)
    app.add_handler(CommandHandler("setgm", cmd_setgm), group=-1)
    app.add_handler(CommandHandler("myid", cmd_myid), group=-1)
    app.add_handler(CommandHandler("whereami", cmd_whereami), group=-1)
    app.add_handler(CommandHandler("locations", cmd_locations), group=-1)

    app.add_handler(report_conv)

    # Диалог /close — в Cash Report групповых чатов
    close_conv = ConversationHandler(
        entry_points=[
            CommandHandler("close", cmd_close),
            CommandHandler("endofshiftreport", cmd_close),
        ],
        states={
            C_TIP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_close_tip),
                CommandHandler("skip", on_close_tip),
            ],
            C_CASH_HOLDING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_close_cash_holding),
                CommandHandler("skip", on_close_cash_holding),
            ],
            C_EXPENSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_close_expenses),
                CommandHandler("skip", on_close_expenses),
            ],
            C_DEFECTIVE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_close_defective),
                CommandHandler("skip", on_close_defective),
            ],
            C_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_close_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_close_cancel)],
        per_chat=False,
    )
    app.add_handler(close_conv)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))

    # Планируем ежедневные проверки отчёта по принтерам для всех известных Shifts-тем
    try:
        schedule_all_printers_checks(app)
    except Exception:
        logger.exception("Не смог запланировать проверки принтеров при старте")

    logger.info("Марса запущена. Жду события смены...")
    app.run_polling()


if __name__ == "__main__":
    main()
