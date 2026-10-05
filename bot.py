import asyncio
import calendar as cal_lib
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import cache
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    ReactionTypeEmoji,
    WebAppInfo,
)
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

if Path(".env").exists():
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Bishkek"))
# Бесплатный лимит Gemini считается отдельно на каждую модель — при 429/503 пробуем следующую.
# Основная — самая быстрая из проверенных (~1–2 с на ответ); более тяжёлые «думают» 10–25 с.
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
          *filter(None, os.getenv("GEMINI_FALLBACKS", "gemini-3.1-flash-lite,gemini-flash-latest").split(","))]
LLM_TIMEOUT_MS = 20_000  # зависшая модель не должна держать ответ — переходим к следующей
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # HTTPS-адрес сервера (туннель или хостинг)
PORT = int(os.getenv("PORT", "8080"))
ALL_DAY_HOUR = 9  # от этого часа считаются напоминания для задач «на весь день»
MAX_OFFSET = 30 * 24 * 60  # самое раннее напоминание — за 30 дней
MAX_REMINDERS = 10
SNOOZE_MINUTES = (15, 60)
SERIES_HORIZON = timedelta(days=60)  # на сколько вперёд создаются повторы
EVENT_LENGTH = timedelta(hours=1)  # считаем, что у задачи со временем длительность час
REPEATS = {"": "", "daily": "каждый день", "weekdays": "по будням", "weekly": "каждую неделю", "monthly": "каждый месяц"}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
COMMANDS = [
    BotCommand(command="today", description="Задачи на сегодня"),
    BotCommand(command="tasks", description="Ближайшие задачи"),
    BotCommand(command="settings", description="Напоминания, утренний план и вечерний итог"),
    BotCommand(command="calendar", description="Показывать задачи в календаре телефона"),
    BotCommand(command="help", description="Что умеет бот"),
]
GROUP_COMMANDS = [
    BotCommand(command="task", description="Ответом на сообщение, голосовое или фото — сделать задачу"),
    BotCommand(command="help", description="Как я работаю в чате"),
]
GROUP = F.chat.type.in_({"group", "supergroup"})

# Тексты профиля бота: выставляются при запуске, в BotFather вводить не нужно.
SHORT_DESCRIPTION = "Собираю дела из чатов, голосовых и скриншотов в календарь и напоминаю вовремя. ИИ-ассистент в Telegram."
DESCRIPTION = (
    "Забываешь договорённости в куче чатов? Я помогу.\n\n"
    "📨 Перешли сообщение, надиктуй голосовое или пришли скриншот — найду встречи, дедлайны и дела\n"
    "🗓 Сложу всё в календарь: прямо в Telegram и в календаре телефона\n"
    "⏰ Напомню, когда нужно, а утром пришлю план на день\n"
    "💬 Спроси: «что у меня завтра?» или «перенеси встречу на субботу»\n\n"
    "Нажми «Старт» 👇"
)

# Дешёвый фильтр для групп: в LLM идут только сообщения с намёком на дату или дело.
HINT = re.compile(
    r"\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)"
    r"|сегодня|завтра|понедельник|вторник|сред[ау]|четверг|пятниц|суббот|воскресен"
    r"|встреч|созвон|митап|дедлайн|забрать|забери|не забудь|перенес|перенос|отмен",
    re.I,
)

PROMPT = """Ты — ассистент-календарь в Telegram. Сейчас {now} ({weekday}).
{mode}

Задачи пользователя (id | начало | название | место | описание | отметки):
{tasks}

Что делать с сообщением:
1. Новое дело (встреча, созвон, митап, дедлайн, «забрать/купить/отправить») → create:
   - title: коротко, до 60 символов, на языке сообщения;
   - start: "YYYY-MM-DDTHH:MM", если время известно, иначе "YYYY-MM-DD"; относительные даты («завтра», «в пятницу»)
     считай от момента отправки сообщения: {ref}; если даты нет — дата сообщения;
   - location: место или "";
   - description: важные детали одной-двумя фразами (с кем, что взять, номер заказа, ссылка) или "";
   - reminders: за сколько минут до начала напомнить, по типу дела: самолёт/поезд — [1440, 180];
     встреча/созвон — [60, 10]; врач, важная встреча — [1440, 60]; дедлайн — [1440, 180];
     «забрать/купить» без времени — [0]; не уверен — [];
   - repeat: "daily", "weekdays", "weekly" или "monthly", если сказано «каждый день/по будням/каждую неделю/каждый месяц»
     или «каждый вторник» и т.п. (тогда start — ближайшее такое время), иначе "".
2. Сообщение про дело, которое уже есть в списке (перенос, отмена, новые детали), или просьба изменить/удалить/отметить
   задачу → update с id этой задачи. В title/start/location/description — только НОВЫЕ значения, остальное "".
   Отмена или «удали» → delete=true. «Сделал», «отметь выполненным» → done=true. Не создавай дубликат существующей задачи.
3. Вопрос о расписании («что у меня завтра», «когда я свободен в четверг», «что я обещал Мише») → ответ в reply:
   коротко, по-русски, простым текстом без markdown, только по списку задач.
4. Если непонятно, какую задачу менять, — спроси в reply и ничего не меняй.
5. Сам ничего не меняешь: create/update пользователь подтверждает кнопкой, поэтому не пиши в reply «удалил», «перенёс».
Пустые create/update/reply — если ничего из этого нет. Ничего не выдумывай.

Сообщение:
{text}"""

db = sqlite3.connect(os.getenv("DB_PATH", "tasks.db"))
db.executescript("""
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    start TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS changes (  -- предложенные ИИ правки, ждут подтверждения
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    fields TEXT NOT NULL
);
""")
for table, column in [
    ("tasks", "done INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "reminders TEXT NOT NULL DEFAULT '60'"),  # минуты до начала через запятую: "1440,60"
    ("tasks", "sent TEXT NOT NULL DEFAULT ''"),  # какие из них уже отправлены
    ("tasks", "snooze TEXT"),  # «напомнить позже»: когда повторить
    ("tasks", "description TEXT NOT NULL DEFAULT ''"),
    ("tasks", "repeat TEXT NOT NULL DEFAULT ''"),  # ключ из REPEATS
    ("tasks", "series INTEGER"),  # id первой задачи серии повторов
    ("tasks", "chat_id INTEGER"),  # группа, из которой задача пришла
    ("users", "reminders TEXT NOT NULL DEFAULT '60'"),  # набор по умолчанию для новых задач
    ("users", "smart_reminders INTEGER NOT NULL DEFAULT 1"),  # ИИ подбирает напоминания под тип дела
    ("users", "digest_hour INTEGER DEFAULT 9"),  # час утреннего плана, NULL — выключен
    ("users", "digest_sent TEXT"),
    ("users", "evening_hour INTEGER DEFAULT 21"),  # час вечернего итога, NULL — выключен
    ("users", "evening_sent TEXT"),
]:
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    except sqlite3.OperationalError:  # колонка уже есть
        pass

TASK_COLUMNS = {"title", "start", "location", "description", "reminders", "sent", "snooze", "done", "repeat", "status"}

dp = Dispatcher()


class NewTask(BaseModel):
    title: str
    start: str
    location: str
    description: str
    reminders: list[int]
    repeat: str


class Change(BaseModel):
    id: int
    title: str
    start: str
    location: str
    description: str
    done: bool
    delete: bool


class Result(BaseModel):
    reply: str
    create: list[NewTask]
    update: list[Change]


@cache
def llm() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": LLM_TIMEOUT_MS})


# ── Даты и подписи ───────────────────────────────────────────────

def normalize(start: str) -> str | None:
    """Приводит дату к "YYYY-MM-DDTHH:MM" или "YYYY-MM-DD"; None — не дата."""
    try:
        d = datetime.fromisoformat(start)
    except ValueError:
        return None
    return f"{d:%Y-%m-%dT%H:%M}" if "T" in start else f"{d:%Y-%m-%d}"


def fmt_like(d: datetime, start: str) -> str:
    """Дата в том же виде, что start: со временем или без."""
    return f"{d:%Y-%m-%dT%H:%M}" if "T" in start else f"{d:%Y-%m-%d}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def human(start: str) -> str:
    d = datetime.fromisoformat(start)
    today = datetime.now(TZ).date()
    day = {today: "Сегодня", today + timedelta(days=1): "Завтра"}.get(
        d.date(), f"{WD_SHORT[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"
    )
    return f"{day}, {d:%H:%M}" if "T" in start else day


def parse_offsets(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x]


def clean_offsets(value) -> str | None:
    """Проверяет список минут; None — мусор."""
    if not isinstance(value, list) or len(value) > MAX_REMINDERS:
        return None
    if not all(type(m) is int and 0 <= m <= MAX_OFFSET for m in value):
        return None
    return ",".join(map(str, sorted(set(value), reverse=True)))


def offset_label(m: int) -> str:
    if m == 0:
        return "в момент начала"
    if m == 7 * 1440:
        return "за неделю"
    if m % 1440 == 0:
        d = m // 1440
        return "за день" if d == 1 else f"за {d} {plural(d, 'день', 'дня', 'дней')}"
    h, mm = divmod(m, 60)
    return "за " + " ".join(filter(None, [h and f"{h} ч", mm and f"{mm} мин"]))


def time_left(delta: timedelta) -> str:
    mins = round(delta.total_seconds() / 60)
    if mins <= 1:
        return "Начинается сейчас"
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    if d:
        return f"Через {d} {plural(d, 'день', 'дня', 'дней')}" + (f" {h} ч" if h else "")
    return "Через " + " ".join(filter(None, [h and f"{h} ч", m and f"{m} мин"]))


def event_time(start: str) -> datetime:
    d = datetime.fromisoformat(start)
    return (d if "T" in start else d.replace(hour=ALL_DAY_HOUR)).replace(tzinfo=TZ)


def due_offsets(start: str, offsets: list[int], sent: set[int], now: datetime) -> list[int]:
    """Напоминания, время которых пришло. После начала события (с запасом 2 мин) — уже ничего."""
    t = event_time(start)
    if now > t + timedelta(minutes=2):
        return []
    return [m for m in offsets if m not in sent and now >= t - timedelta(minutes=m)]


def next_occurrence(d: datetime, repeat: str) -> datetime:
    if repeat == "daily":
        return d + timedelta(days=1)
    if repeat == "weekdays":
        d += timedelta(days=1)
        while d.weekday() > 4:
            d += timedelta(days=1)
        return d
    if repeat == "weekly":
        return d + timedelta(days=7)
    # monthly; ponytail: 31-е в коротком месяце сдвигается на последний день и дальше остаётся там — хранить исходный день, если важно.
    y, m = divmod(d.month, 12)
    y, m = d.year + y, m + 1
    return d.replace(year=y, month=m, day=min(d.day, cal_lib.monthrange(y, m)[1]))


# ── Внешние календари ────────────────────────────────────────────

def gcal_link(title: str, start: str, location: str, description: str = "") -> str:
    s = datetime.fromisoformat(start)
    if "T" in start:
        dates = f"{s:%Y%m%dT%H%M%S}/{s + EVENT_LENGTH:%Y%m%dT%H%M%S}"
    else:
        dates = f"{s:%Y%m%d}/{s + timedelta(days=1):%Y%m%d}"
    query = {"action": "TEMPLATE", "text": title, "dates": dates, "location": location, "details": description, "ctz": TZ.key}
    return "https://calendar.google.com/calendar/render?" + urlencode(query)


def cal_path(uid: int) -> str:
    # ponytail: подпись от BOT_TOKEN — после /revoke токена старые ссылки на календарь перестанут работать.
    sig = hmac.new(os.environ["BOT_TOKEN"].encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:32]
    return f"cal/{uid}-{sig}.ics"


def cal_links(uid: int) -> dict[str, str]:
    url = f"{PUBLIC_URL}/{cal_path(uid)}"
    webcal = "webcal://" + url.split("://", 1)[1]
    return {
        "url": url,
        "apple": f"{PUBLIC_URL}/subscribe/{cal_path(uid)}",  # редирект на webcal:// — Telegram не открывает его напрямую
        "google": "https://calendar.google.com/calendar/render?" + urlencode({"cid": webcal}),
    }


def ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(rows: list[tuple[int, str, str, str, str]]) -> str:
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Task Tracker//RU", "X-WR-CALNAME:Задачи из Telegram",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M", "X-PUBLISHED-TTL:PT15M",
    ]
    for task_id, title, start, location, description in rows:
        s = datetime.fromisoformat(start)
        if "T" in start:
            s = s.replace(tzinfo=TZ).astimezone(timezone.utc)
            when = [f"DTSTART:{s:%Y%m%dT%H%M%SZ}", f"DTEND:{s + EVENT_LENGTH:%Y%m%dT%H%M%SZ}"]
        else:
            when = [f"DTSTART;VALUE=DATE:{s:%Y%m%d}", f"DTEND;VALUE=DATE:{s + timedelta(days=1):%Y%m%d}"]
        lines += [
            "BEGIN:VEVENT", f"UID:{task_id}@tasktracker", f"DTSTAMP:{stamp}", *when,
            f"SUMMARY:{ics_escape(title)}", f"LOCATION:{ics_escape(location)}",
            f"DESCRIPTION:{ics_escape(description)}", "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    # ponytail: длинные строки не переносятся по 75 байт (RFC 5545) — Apple и Google это терпят; добавить, если клиент споткнётся.
    return "\r\n".join(lines) + "\r\n"


# ── Задачи в базе ────────────────────────────────────────────────

def register(uid: int) -> None:
    db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
    db.commit()


def clean_fields(data: dict) -> dict | None:
    """Проверенные поля задачи (из Mini App или от ИИ); None — что-то некорректно."""
    fields = {}
    if "title" in data:
        if not (title := str(data["title"]).strip()[:100]):
            return None
        fields["title"] = title
    if "location" in data:
        fields["location"] = str(data["location"]).strip()[:100]
    if "description" in data:
        fields["description"] = str(data["description"]).strip()[:500]
    if "start" in data:
        if not (start := normalize(str(data["start"]))):
            return None
        fields |= {"start": start, "sent": "", "snooze": None}
    if "reminders" in data:
        if (reminders := clean_offsets(data["reminders"])) is None:
            return None
        fields |= {"reminders": reminders, "sent": ""}
    if "done" in data:
        fields["done"] = int(bool(data["done"]))
    if "repeat" in data:
        if data["repeat"] not in REPEATS:
            return None
        fields["repeat"] = data["repeat"]
    return fields


def update_task(task_id: int, uid: int, fields: dict) -> None:
    assert set(fields) <= TASK_COLUMNS  # имена колонок подставляются в SQL — только из белого списка
    if fields:
        db.execute(
            f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? AND user_id = ?",
            (*fields.values(), task_id, uid),
        )
        db.commit()


def extend_series(series: int, until: datetime) -> None:
    """Достраивает повторы серии до даты until, копируя последнюю задачу серии."""
    row = db.execute(
        "SELECT user_id, title, start, location, description, source, chat_id, reminders, repeat FROM tasks"
        " WHERE series = ? ORDER BY start DESC LIMIT 1",
        (series,),
    ).fetchone()
    if not row or row[8] not in REPEATS or not row[8]:
        return
    uid, title, start, location, description, source, chat_id, reminders, repeat = row
    d = datetime.fromisoformat(start)
    while (d := next_occurrence(d, repeat)) <= until.replace(tzinfo=None):
        db.execute(
            "INSERT INTO tasks (user_id, title, start, location, description, source, chat_id, reminders, repeat,"
            " series, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')",
            (uid, title, fmt_like(d, start), location, description, source, chat_id, reminders, repeat, series),
        )
    db.commit()


def start_series(task_id: int) -> None:
    """Подтверждённая задача с повтором становится началом серии."""
    if db.execute(
        "UPDATE tasks SET series = id WHERE id = ? AND repeat != '' AND series IS NULL AND status = 'ok'", (task_id,)
    ).rowcount:
        extend_series(task_id, datetime.now(TZ) + SERIES_HORIZON)


def conflicts(uid: int, start: str, exclude: int = 0) -> list[tuple[str, str]]:
    """Задачи со временем, которые пересекаются с новой (каждая длится EVENT_LENGTH)."""
    if "T" not in start:
        return []
    t = datetime.fromisoformat(start)
    return db.execute(
        "SELECT title, start FROM tasks WHERE user_id = ? AND id != ? AND status = 'ok' AND done = 0"
        " AND start LIKE '%T%' AND start > ? AND start < ? ORDER BY start",
        (uid, exclude, fmt_like(t - EVENT_LENGTH, start), fmt_like(t + EVENT_LENGTH, start)),
    ).fetchall()


# ── Тексты и кнопки ──────────────────────────────────────────────

def card(title: str, start: str, location: str = "", source: str = "", description: str = "") -> str:
    """HTML-карточка задачи; всё пользовательское экранируется."""
    lines = [
        f"<b>{escape(title)}</b>",
        f"🗓 {human(start)}",
        location and f"📍 {escape(location)}",
        description and f"📝 {escape(description)}",
        source and f"💬 {escape(source)}",
    ]
    return "\n".join(filter(None, lines))


def extras(uid: int, task_id: int, start: str, reminders: str, repeat: str) -> str:
    """Строки под карточкой: напоминания, повтор, пересечения."""
    offsets = parse_offsets(reminders)
    lines = ["🔔 Напомню " + ", ".join(map(offset_label, offsets)) if offsets else "🔕 Без напоминаний"]
    if repeat:
        lines.append(f"🔁 Повторять {REPEATS[repeat]}")
    if clash := conflicts(uid, start, task_id):
        lines.append("⚠️ В это время уже: " + ", ".join(f"{s[11:16]} {escape(t)}" for t, s in clash))
    return "\n".join(lines)


def task_text(task_id: int, uid: int) -> str:
    title, start, location, source, description, reminders, repeat = db.execute(
        "SELECT title, start, location, source, description, reminders, repeat FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return card(title, start, location, source, description) + "\n" + extras(uid, task_id, start, reminders, repeat)


def app_button(text: str = "🗓 Открыть календарь", **query) -> list[InlineKeyboardButton]:
    """Кнопка Mini App; query открывает нужный экран: task+date — задачу, view=settings — настройки."""
    if not PUBLIC_URL:
        return []
    url = f"{PUBLIC_URL}/?{urlencode(query)}" if query else PUBLIC_URL
    return [InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    """Клавиатура без пустых рядов (Telegram их не принимает)."""
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ── ИИ ───────────────────────────────────────────────────────────

def context_lines(rows) -> str:
    lines = []
    for task_id, start, title, location, description, done, status in rows:
        marks = ", ".join(filter(None, [done and "выполнено", status == "pending" and "ждёт подтверждения"]))
        lines.append(" | ".join([f"#{task_id}", start, title, location or "-", description or "-", marks or "-"]))
    return "\n".join(lines) or "(задач нет)"


def user_context(uid: int) -> str:
    rows = db.execute(
        "SELECT id, start, title, location, description, done, status FROM tasks"
        " WHERE user_id = ? AND start >= ? ORDER BY start LIMIT 80",
        (uid, f"{datetime.now(TZ) - timedelta(days=7):%Y-%m-%d}"),
    ).fetchall()
    return context_lines(rows)


def group_context(chat_id: int) -> str:
    """Дела, уже найденные в этой группе; у каждого пользователя своя копия — показываем по одной."""
    rows = db.execute(
        "SELECT MIN(id), start, title, location, description, 0, 'ok' FROM tasks"
        " WHERE chat_id = ? AND start >= ? GROUP BY title, start ORDER BY start LIMIT 50",
        (chat_id, f"{datetime.now(TZ):%Y-%m-%d}"),
    ).fetchall()
    return context_lines(rows)


async def media_parts(m: Message) -> list:
    """Голосовое, фото или картинка-файл — в Gemini как есть."""
    if m.voice:
        file, mime = m.voice, "audio/ogg"
    elif m.photo:
        file, mime = m.photo[-1], "image/jpeg"
    elif m.document and (m.document.mime_type or "").startswith("image/"):
        file, mime = m.document, m.document.mime_type
    else:
        return []
    data = await m.bot.download(file)
    return [types.Part.from_bytes(data=data.read(), mime_type=mime)]


async def analyze(m: Message, mode: str, tasks: str) -> Result | None:
    """Один запрос к Gemini: что сделать с сообщением. None — ошибка."""
    now = datetime.now(TZ)
    ref = (m.forward_origin.date if m.forward_origin else m.date).astimezone(TZ)
    attached = "голосовое" if m.voice else "изображение"
    text = m.text or m.caption or f"(текста нет — {attached} во вложении)"
    prompt = PROMPT.format(
        now=f"{now:%Y-%m-%d %H:%M}", weekday=WEEKDAYS[now.weekday()], mode=mode, tasks=tasks,
        ref=f"{ref:%Y-%m-%d %H:%M} ({WEEKDAYS[ref.weekday()]})", text=text,
    )
    try:
        contents = [prompt, *await media_parts(m)]
        for attempt in range(2):
            for model in MODELS:
                try:
                    resp = await llm().aio.models.generate_content(
                        model=model,
                        contents=contents,
                        config={"response_mime_type": "application/json", "response_schema": Result},
                    )
                    return resp.parsed
                except errors.APIError as e:
                    if e.code not in (404, 429, 500, 503):  # нет модели, лимит или перегрузка — пробуем дальше
                        raise
                    logging.warning("gemini %s: %s", model, e.code)
                except TimeoutError:
                    logging.warning("gemini %s: timeout", model)
            await asyncio.sleep(5)
    except Exception as e:  # сеть, неверный ключ
        logging.warning("gemini: %s", e)
    return None


def change_data(c: Change) -> dict:
    if c.delete:
        return {"delete": True}
    data = {k: v for k, v in (("title", c.title), ("start", c.start), ("location", c.location),
                              ("description", c.description)) if v}
    return data | ({"done": True} if c.done else {})


async def create_pending(bot: Bot, uid: int, t: NewTask, source: str, chat_id: int | None) -> bool:
    if not (start := normalize(t.start)) or not t.title.strip():
        return False
    smart, default = db.execute("SELECT smart_reminders, reminders FROM users WHERE id = ?", (uid,)).fetchone() or (1, "60")
    reminders = (smart and t.reminders and clean_offsets(t.reminders)) or default
    task_id = db.execute(
        "INSERT INTO tasks (user_id, title, start, location, description, source, chat_id, reminders, repeat)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, t.title.strip()[:100], start, t.location[:100], t.description[:500], source, chat_id, reminders,
         t.repeat if t.repeat in REPEATS else ""),
    ).lastrowid
    db.commit()
    kb = keyboard([
        InlineKeyboardButton(text="✅ Добавить", callback_data=f"ok:{task_id}"),
        InlineKeyboardButton(text="✕", callback_data=f"no:{task_id}"),
    ])
    await bot.send_message(uid, task_text(task_id, uid), parse_mode="HTML", reply_markup=kb)
    return True


def change_text(uid: int, task_id: int, data: dict) -> str | None:
    """Описание правки «было → стало»; None — менять нечего."""
    title, start, location, description, done = db.execute(
        "SELECT title, start, location, description, done FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if data.get("delete"):
        return "🗑 <b>Удалить задачу?</b>\n\n" + card(title, start, location, description=description)
    lines = []
    if data.get("title", title) != title:
        lines.append(f"📌 {escape(title)} → <b>{escape(data['title'])}</b>")
    if data.get("start", start) != start:
        lines.append(f"🗓 {human(start)} → <b>{human(data['start'])}</b>")
        if clash := conflicts(uid, data["start"], task_id):
            lines.append("⚠️ В это время уже: " + ", ".join(f"{s[11:16]} {escape(t)}" for t, s in clash))
    if data.get("location", location) != location:
        lines.append(f"📍 {escape(location or '—')} → <b>{escape(data['location'])}</b>")
    if data.get("description", description) != description:
        lines.append(f"📝 <b>{escape(data['description'])}</b>")
    if data.get("done") and not done:
        lines.append("✅ Отметить выполненной")
    return f"✏️ <b>Изменить «{escape(title)}»?</b>\n\n" + "\n".join(lines) if lines else None


async def propose_change(bot: Bot, uid: int, task_id: int, data: dict) -> bool:
    """Правка от ИИ не применяется сразу — пользователь подтверждает её кнопкой."""
    if not db.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone():
        return False  # ИИ ошибся с id или это чужая задача
    if not data.get("delete") and not (data := clean_fields(data)):
        return False
    if not (text := change_text(uid, task_id, data)):
        return False
    change_id = db.execute(
        "INSERT INTO changes (user_id, task_id, fields) VALUES (?, ?, ?)", (uid, task_id, json.dumps(data))
    ).lastrowid
    db.commit()
    kb = keyboard([
        InlineKeyboardButton(text="✅ Применить", callback_data=f"ch:ok:{change_id}"),
        InlineKeyboardButton(text="✕", callback_data=f"ch:no:{change_id}"),
    ])
    await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
    return True


# ── Команды и сообщения ──────────────────────────────────────────

@dp.message(Command("start", "help"), F.chat.type == "private")
async def start(m: Message):
    register(m.from_user.id)
    name = escape(m.from_user.first_name or "")
    await m.answer(
        f"<b>Привет{', ' + name if name else ''}! Я собираю твои дела из чатов.</b>\n\n"
        "📨 Пересылай мне сообщения про встречи и дела\n"
        "🎙 Надиктуй голосовое: «завтра в 10 стоматолог»\n"
        "📸 Пришли скриншот переписки, афишу или билет\n"
        "👥 Добавь меня в рабочий чат — замечу и новые дела, и переносы\n\n"
        "<b>Можно просто написать</b>\n"
        "• «что у меня завтра?», «когда я свободен в четверг?»\n"
        "• «перенеси встречу с Мишей на субботу», «отмени стоматолога»\n"
        "• «каждый вторник в 10 планёрка»\n\n"
        "⏰ Напомню, когда нужно — сам подберу время под тип дела\n"
        "☀️ Утром пришлю план, 🌙 вечером — что не успел\n\n"
        "<b>Команды</b>\n"
        "/today — задачи на сегодня\n"
        "/tasks — ближайшие задачи\n"
        "/settings — напоминания, утренний план и вечерний итог\n"
        "/calendar — показывать задачи в календаре телефона\n"
        "/help — эта подсказка",
        parse_mode="HTML",
        reply_markup=keyboard(app_button()),
    )


@dp.message(Command("settings"), F.chat.type == "private")
async def settings(m: Message):
    register(m.from_user.id)
    if not PUBLIC_URL:
        await m.answer("Настройки пока недоступны: не задан PUBLIC_URL.")
        return
    await m.answer(
        "Когда и сколько раз напоминать, утренний план и вечерний итог — всё в настройках:",
        reply_markup=keyboard(app_button("⚙️ Открыть настройки", view="settings")),
    )


@dp.message(Command("calendar"), F.chat.type == "private")
async def calendar(m: Message):
    if not PUBLIC_URL:
        await m.answer("Календарь пока недоступен: не задан PUBLIC_URL.")
        return
    links = cal_links(m.from_user.id)
    await m.answer(
        "Подключи календарь один раз — подтверждённые задачи будут появляться в нём сами.\n\n"
        f"Другой календарь — добавь его по ссылке:\n{links['url']}",
        reply_markup=keyboard(
            [InlineKeyboardButton(text="🍎 iPhone / Mac", url=links["apple"])],
            [InlineKeyboardButton(text="📅 Google Календарь", url=links["google"])],
        ),
    )


async def agenda(m: Message, start_from: str, start_to: str, empty: str) -> None:
    rows = db.execute(
        "SELECT title, start FROM tasks WHERE user_id = ? AND status = 'ok' AND done = 0 AND start >= ? AND start < ?"
        " ORDER BY start LIMIT 20",
        (m.from_user.id, start_from, start_to),
    ).fetchall()
    text = "\n".join(f"• {human(s)} — {escape(t)}" for t, s in rows) or empty
    await m.answer(text, parse_mode="HTML", reply_markup=keyboard(app_button()))


@dp.message(Command("today"), F.chat.type == "private")
async def today(m: Message):
    now = datetime.now(TZ)
    await agenda(m, f"{now:%Y-%m-%d}", f"{now + timedelta(days=1):%Y-%m-%d}", "На сегодня задач нет 🎉")


@dp.message(Command("tasks"), F.chat.type == "private")
async def tasks(m: Message):
    await agenda(m, f"{datetime.now(TZ):%Y-%m-%d}", "9999", "Задач нет.")


# ponytail: каждое пересланное сообщение — отдельный запрос к Gemini; при упоре в лимиты склеивать пачку пересылок.
@dp.message(F.chat.type == "private", F.text | F.caption | F.voice | F.photo | F.document)
async def private(m: Message):
    if (m.text or "").startswith("/"):  # неизвестная команда — не тратим запрос к ИИ
        await m.answer("Не знаю такой команды. Список — /help")
        return
    if m.document and not (m.document.mime_type or "").startswith("image/") and not m.caption:
        await m.reply("Пришли текст, голосовое, фото или скриншот — файлы других типов я не читаю.")
        return
    uid = m.from_user.id
    register(uid)
    await m.bot.send_chat_action(m.chat.id, "typing")
    mode = (
        "Пользователь переслал тебе сообщение из другого чата."
        if m.forward_origin
        else "Пользователь пишет тебе сам: это может быть новое дело, просьба изменить задачи или вопрос о расписании."
    )
    res = await analyze(m, mode, user_context(uid))
    if res is None:
        await m.reply("Не получилось разобрать, попробуй позже.")
        return
    acted = 0
    for t in res.create:
        acted += await create_pending(m.bot, uid, t, "", None)
    for c in res.update:
        acted += await propose_change(m.bot, uid, c.id, change_data(c))
    if res.reply and not acted:  # при правках ответ ИИ не нужен — всё видно в карточках
        await m.answer(res.reply)
    elif not acted:
        await m.reply("Не нашёл тут дел. Можно спросить: «что у меня завтра?» или «перенеси встречу на субботу».")


def start_button(bot_username: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="🚀 Получать задачи в личку", url=f"https://t.me/{bot_username}?start=group")]


@dp.message(Command("start", "help"), GROUP)
async def group_help(m: Message):
    me = await m.bot.get_me()
    await m.reply(
        "Я замечаю в этом чате встречи, дедлайны и переносы и присылаю их в личку тем, кто запустил меня. "
        "В чат я ничего не пишу.\n\n"
        "🎙 Голосовые и фото сам не разбираю. Ответь на такое сообщение командой /task — "
        "и я пришлю задачу тебе в личку. Так же можно с любым сообщением, которое я пропустил.",
        reply_markup=keyboard(start_button(me.username)),
    )


@dp.message(Command("today", "tasks", "settings", "calendar"), GROUP)
async def group_private_only(m: Message):
    me = await m.bot.get_me()
    await m.reply("Эта команда работает в личке — там твои задачи не видны другим.",
                  reply_markup=keyboard(start_button(me.username)))


@dp.message(Command("task"), GROUP)
async def group_task(m: Message):
    """/task ответом на сообщение: явная просьба разобрать его (в т.ч. голосовое или фото). Задача — только автору команды."""
    me = await m.bot.get_me()
    uid = m.from_user.id
    if not (target := m.reply_to_message):
        await m.reply("Ответь командой /task на сообщение, голосовое или фото — и я сделаю из него задачу.")
        return
    if not db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
        await m.reply("Сначала нажми «Старт» у меня в личке — туда придёт задача.",
                      reply_markup=keyboard(start_button(me.username)))
        return
    mode = f"Пользователь попросил сделать задачу из сообщения в групповом чате «{m.chat.title or ''}»."
    if (res := await analyze(target.as_(m.bot), mode, user_context(uid))) is None:
        await m.reply("Не получилось разобрать, попробуй позже.")
        return
    acted = 0
    for t in res.create:
        acted += await create_pending(m.bot, uid, t, m.chat.title or "", m.chat.id)
    for c in res.update:
        acted += await propose_change(m.bot, uid, c.id, change_data(c))
    if acted:
        await m.react([ReactionTypeEmoji(emoji="👍")])  # тихо: карточка уже в личке
    else:
        await m.reply("Не нашёл тут дела.")


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION), GROUP)
async def added_to_group(event: ChatMemberUpdated):
    """Бота добавили в группу: объясняем, как он работает, и предупреждаем, если он не видит сообщений."""
    me = await event.bot.get_me()
    text = (
        "👋 Привет! Я замечаю в этом чате встречи, дедлайны и переносы и присылаю их в личку — "
        "тем, кто запустил меня. Здесь я молчу и ничего не пишу.\n\n"
        "Чтобы получать задачи из этого чата — нажмите кнопку ниже и «Старт»."
    )
    if not me.can_read_all_group_messages and event.new_chat_member.status != "administrator":
        text += (
            "\n\n⚠️ Сейчас я не вижу сообщений чата. Сделайте меня администратором "
            "или отключите режим приватности в @BotFather (/setprivacy → Disable) и добавьте меня заново."
        )
    await event.answer(text + "\n\n🎙 Голосовое или фото — ответьте на него командой /task.",
                       reply_markup=keyboard(start_button(me.username)))


@dp.message(GROUP, F.text | F.caption)
async def group(m: Message):
    if not HINT.search(m.text or m.caption):
        return
    mode = f"Сообщение из группового чата «{m.chat.title or ''}». В списке — дела, уже найденные в этом чате."
    if not (res := await analyze(m, mode, group_context(m.chat.id))):
        return
    if res.create:
        # ponytail: перебор всех пользователей бота с проверкой членства — O(users) запросов; при росте хранить связку чат↔пользователь.
        for (uid,) in db.execute("SELECT id FROM users").fetchall():
            try:
                if (await m.bot.get_chat_member(m.chat.id, uid)).status in ("left", "kicked"):
                    continue
                for t in res.create:
                    await create_pending(m.bot, uid, t, m.chat.title or "", m.chat.id)
            except TelegramAPIError as e:  # пользователь не в чате или заблокировал бота
                logging.info("skip %s: %s", uid, e)
    for c in res.update:  # перенос/отмена — каждому, у кого есть копия этой задачи
        if not (rep := db.execute("SELECT title, start FROM tasks WHERE id = ? AND chat_id = ?", (c.id, m.chat.id)).fetchone()):
            continue
        for task_id, uid in db.execute(
            "SELECT id, user_id FROM tasks WHERE chat_id = ? AND title = ? AND start = ?", (m.chat.id, *rep)
        ).fetchall():
            try:
                await propose_change(m.bot, uid, task_id, change_data(c))
            except TelegramAPIError as e:
                logging.info("skip %s: %s", uid, e)


# ── Кнопки ───────────────────────────────────────────────────────

@dp.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def decide(c: CallbackQuery):
    action, task_id = c.data.split(":")
    uid = c.from_user.id
    row = db.execute(
        "SELECT start FROM tasks WHERE id = ? AND user_id = ? AND status = 'pending'", (task_id, uid)
    ).fetchone()
    if not row:
        await c.answer("Уже обработано")
        return
    if action == "no":
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        db.commit()
        await c.answer()
        await c.message.delete()
        return
    db.execute("UPDATE tasks SET status = 'ok' WHERE id = ?", (task_id,))
    db.commit()
    start_series(int(task_id))
    title, start, location, description = db.execute(
        "SELECT title, start, location, description FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    await c.answer("Сохранено")
    await c.message.edit_text(
        "✅ " + task_text(int(task_id), uid),
        parse_mode="HTML",
        reply_markup=keyboard(
            app_button("✏️ Изменить и настроить напоминания", task=task_id, date=start[:10]),
            [InlineKeyboardButton(text="📅 В Google Календарь", url=gcal_link(title, start, location, description))],
        ),
    )


@dp.callback_query(F.data.regexp(r"^ch:(ok|no):\d+$"))
async def change_action(c: CallbackQuery):
    _, action, change_id = c.data.split(":")
    uid = c.from_user.id
    row = db.execute("SELECT task_id, fields FROM changes WHERE id = ? AND user_id = ?", (change_id, uid)).fetchone()
    db.execute("DELETE FROM changes WHERE id = ? AND user_id = ?", (change_id, uid))
    db.commit()
    task_id, data = row if row else (None, {})
    exists = task_id and db.execute("SELECT title FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone()
    if not exists:
        await c.answer("Уже неактуально")
        await c.message.edit_reply_markup(reply_markup=None)
        return
    if action == "no":
        await c.answer("Оставил как было")
        await c.message.delete()
        return
    data = json.loads(data)
    if data.get("delete"):
        db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid))
        db.commit()
        await c.answer("Удалено")
        await c.message.edit_text(f"🗑 Удалено: <b>{escape(exists[0])}</b>", parse_mode="HTML")
        return
    update_task(task_id, uid, data)
    await c.answer("Готово")
    await c.message.edit_text("✅ <b>Обновлено</b>\n\n" + task_text(task_id, uid), parse_mode="HTML")


@dp.callback_query(F.data.regexp(r"^(sz:(15|60)|dn):\d+$"))
async def reminder_action(c: CallbackQuery):
    *action, task_id = c.data.split(":")
    row = db.execute(
        "SELECT title, start, location FROM tasks WHERE id = ? AND user_id = ?", (task_id, c.from_user.id)
    ).fetchone()
    if not row:
        await c.answer("Задача удалена")
        return
    if action[0] == "dn":
        db.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
        db.commit()
        await c.answer("Отлично!")
        await c.message.edit_text("✅ <b>Сделано</b>\n\n" + card(*row), parse_mode="HTML")
        return
    at = datetime.now(TZ) + timedelta(minutes=int(action[1]))
    db.execute("UPDATE tasks SET snooze = ? WHERE id = ?", (f"{at:%Y-%m-%dT%H:%M}", task_id))
    db.commit()
    await c.answer(f"Напомню в {at:%H:%M}")
    await c.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.regexp(r"^ev:(mv|dn):\d{4}-\d{2}-\d{2}$"))
async def evening_action(c: CallbackQuery):
    _, action, day = c.data.split(":")
    uid = c.from_user.id
    rows = db.execute(
        "SELECT id, start FROM tasks WHERE user_id = ? AND status = 'ok' AND done = 0 AND substr(start, 1, 10) = ?",
        (uid, day),
    ).fetchall()
    for task_id, start in rows:
        if action == "dn":
            update_task(task_id, uid, {"done": 1})
        else:
            update_task(task_id, uid, {"start": fmt_like(datetime.fromisoformat(start) + timedelta(days=1), start),
                                       "sent": "", "snooze": None})
    n = len(rows)
    text = (f"📅 Перенёс на завтра: {n} {plural(n, 'дело', 'дела', 'дел')}" if action == "mv"
            else f"✅ Отметил выполненными: {n} {plural(n, 'дело', 'дела', 'дел')}")
    await c.answer()
    await c.message.edit_text(text, reply_markup=keyboard(app_button()))


# ── Планировщик ──────────────────────────────────────────────────

async def notify(bot: Bot, uid: int, task_id: int, title: str, start: str, location: str, header: str) -> None:
    kb = keyboard([
        *[InlineKeyboardButton(text=f"⏰ +{m} мин" if m < 60 else f"⏰ +{m // 60} ч", callback_data=f"sz:{m}:{task_id}")
          for m in SNOOZE_MINUTES],
        InlineKeyboardButton(text="✅ Сделано", callback_data=f"dn:{task_id}"),
    ])
    try:
        await bot.send_message(uid, f"⏰ <b>{header}</b>\n\n" + card(title, start, location), parse_mode="HTML", reply_markup=kb)
    except TelegramAPIError as e:
        logging.info("reminder %s: %s", uid, e)


async def send_reminders(bot: Bot, now: datetime) -> None:
    # ponytail: каждую минуту перебираем задачи на месяц вперёд в Python; при росте — таблица напоминаний с индексом по времени.
    rows = db.execute(
        "SELECT id, user_id, title, start, location, reminders, sent FROM tasks"
        " WHERE status = 'ok' AND done = 0 AND reminders != '' AND start BETWEEN ? AND ?",
        (f"{now - timedelta(days=1):%Y-%m-%d}", f"{now + timedelta(days=31):%Y-%m-%d}"),
    ).fetchall()
    for task_id, uid, title, start, location, reminders, sent in rows:
        sent_set = set(parse_offsets(sent))
        if not (due := due_offsets(start, parse_offsets(reminders), sent_set, now)):
            continue
        # Несколько просроченных сразу (бот был выключен) — одно сообщение, а не пачка.
        db.execute("UPDATE tasks SET sent = ? WHERE id = ?", (",".join(map(str, sent_set | set(due))), task_id))
        db.commit()
        header = time_left(event_time(start) - now) if "T" in start else "Напоминание"
        await notify(bot, uid, task_id, title, start, location, header)


async def send_snoozed(bot: Bot, now: datetime) -> None:
    rows = db.execute(
        "SELECT id, user_id, title, start, location FROM tasks WHERE snooze <= ? AND done = 0",
        (f"{now:%Y-%m-%dT%H:%M}",),
    ).fetchall()
    for task_id, uid, title, start, location in rows:
        db.execute("UPDATE tasks SET snooze = NULL WHERE id = ?", (task_id,))
        db.commit()
        await notify(bot, uid, task_id, title, start, location, "Напоминаю ещё раз")


def due_users(hour_column: str, sent_column: str, now: datetime) -> list[int]:
    """Пользователи, которым в этот час пора прислать сводку; сразу помечаем, чтобы не прислать дважды."""
    today = f"{now:%Y-%m-%d}"
    uids = [uid for (uid,) in db.execute(
        f"SELECT id FROM users WHERE {hour_column} = ? AND {sent_column} IS NOT ?", (now.hour, today)
    )]
    db.executemany(f"UPDATE users SET {sent_column} = ? WHERE id = ?", [(today, uid) for uid in uids])
    db.commit()
    return uids


def day_tasks(uid: int, day: str) -> list[tuple[str, str]]:
    return db.execute(
        "SELECT title, start FROM tasks WHERE user_id = ? AND status = 'ok' AND done = 0 AND substr(start, 1, 10) = ?"
        " ORDER BY start",
        (uid, day),
    ).fetchall()


async def send_digests(bot: Bot, now: datetime) -> None:
    for uid in due_users("digest_hour", "digest_sent", now):
        if not (rows := day_tasks(uid, f"{now:%Y-%m-%d}")):
            continue
        lines = [f"• {start[11:16] or 'весь день'} — {escape(title)}" for title, start in rows]
        try:
            await bot.send_message(
                uid, "☀️ <b>Доброе утро! План на сегодня:</b>\n\n" + "\n".join(lines),
                parse_mode="HTML", reply_markup=keyboard(app_button()),
            )
        except TelegramAPIError as e:
            logging.info("digest %s: %s", uid, e)


async def send_evenings(bot: Bot, now: datetime) -> None:
    day = f"{now:%Y-%m-%d}"
    for uid in due_users("evening_hour", "evening_sent", now):
        if not (rows := day_tasks(uid, day)):
            continue
        n = len(rows)
        lines = [f"• {start[11:16] or 'весь день'} — {escape(title)}" for title, start in rows]
        try:
            await bot.send_message(
                uid,
                f"🌙 <b>Сегодня не отмечено {n} {plural(n, 'дело', 'дела', 'дел')}:</b>\n\n" + "\n".join(lines),
                parse_mode="HTML",
                reply_markup=keyboard(
                    [InlineKeyboardButton(text="📅 Всё на завтра", callback_data=f"ev:mv:{day}"),
                     InlineKeyboardButton(text="✅ Всё сделано", callback_data=f"ev:dn:{day}")],
                    app_button("🗓 Разобрать в календаре", date=day),
                ),
            )
        except TelegramAPIError as e:
            logging.info("evening %s: %s", uid, e)


def extend_all_series(now: datetime) -> None:
    until = now + SERIES_HORIZON
    for (series,) in db.execute(
        "SELECT series FROM tasks WHERE series IS NOT NULL GROUP BY series HAVING MAX(start) < ?",
        (f"{until - timedelta(days=7):%Y-%m-%d}",),
    ).fetchall():
        extend_series(series, until)


async def scheduler(bot: Bot) -> None:
    while True:
        now = datetime.now(TZ)
        try:
            await send_reminders(bot, now)
            await send_snoozed(bot, now)
            await send_digests(bot, now)
            await send_evenings(bot, now)
            extend_all_series(now)
        except Exception:
            logging.exception("scheduler")
        await asyncio.sleep(60)


# ── Веб: календарная подписка и API Mini App ─────────────────────

def feed_uid(request: web.Request) -> int:
    name = request.match_info["name"]
    uid = name.split("-", 1)[0]
    if not uid.isdigit() or not hmac.compare_digest(f"cal/{name}", cal_path(int(uid))):
        raise web.HTTPNotFound()
    return int(uid)


async def feed(request: web.Request) -> web.Response:
    rows = db.execute(
        "SELECT id, title, start, location, description FROM tasks WHERE user_id = ? AND status = 'ok'",
        (feed_uid(request),),
    ).fetchall()
    return web.Response(text=ics(rows), content_type="text/calendar", charset="utf-8")


async def subscribe(request: web.Request) -> web.Response:
    feed_uid(request)
    raise web.HTTPFound("webcal://" + f"{PUBLIC_URL}/cal/{request.match_info['name']}".split("://", 1)[1])


def webapp_user(request: web.Request) -> int:
    try:
        user = safe_parse_webapp_init_data(os.environ["BOT_TOKEN"], request.headers.get("Authorization", "")).user
    except ValueError:
        user = None
    if not user:
        raise web.HTTPUnauthorized()
    return user.id


async def json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise web.HTTPBadRequest()
    if not isinstance(data, dict):
        raise web.HTTPBadRequest()
    return data


def fields_or_400(data: dict) -> dict:
    if (fields := clean_fields(data)) is None:
        raise web.HTTPBadRequest()
    return fields


async def api_tasks(request: web.Request) -> web.Response:
    """Подтверждённые задачи за период [from, to) и все неподтверждённые."""
    uid = webapp_user(request)
    register(uid)
    start_from = normalize(request.query.get("from", "")) or f"{datetime.now(TZ):%Y-%m-%d}"
    start_to = normalize(request.query.get("to", "")) or "9999"
    rows = db.execute(
        "SELECT id, title, start, location, description, source, status, done, repeat, series, reminders FROM tasks"
        " WHERE user_id = ? AND (status = 'pending' OR (start >= ? AND start < ?)) ORDER BY start",
        (uid, start_from, start_to),
    ).fetchall()
    keys = ("id", "title", "start", "location", "description", "source", "status", "done", "repeat", "series")
    reminders, smart, digest, evening = db.execute(
        "SELECT reminders, smart_reminders, digest_hour, evening_hour FROM users WHERE id = ?", (uid,)
    ).fetchone()
    return web.json_response({
        "tasks": [dict(zip(keys, r)) | {"reminders": parse_offsets(r[10])} for r in rows],
        "settings": {"reminders": parse_offsets(reminders), "smart_reminders": bool(smart),
                     "digest_hour": digest, "evening_hour": evening},
        "calendar": cal_links(uid),
    })


async def api_create(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    fields = fields_or_400(await json_body(request))
    if "title" not in fields or "start" not in fields:
        raise web.HTTPBadRequest()
    register(uid)
    fields.setdefault("reminders", db.execute("SELECT reminders FROM users WHERE id = ?", (uid,)).fetchone()[0])
    fields |= {"status": "ok"}
    assert set(fields) <= TASK_COLUMNS
    task_id = db.execute(
        f"INSERT INTO tasks (user_id, {', '.join(fields)}) VALUES (?, {', '.join('?' * len(fields))})",
        (uid, *fields.values()),
    ).lastrowid
    db.commit()
    start_series(task_id)
    return web.json_response({"id": task_id})


async def api_update(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    task_id = int(request.match_info["id"])
    data = await json_body(request)
    row = db.execute("SELECT start, series FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone()
    if not row:
        raise web.HTTPNotFound()
    start, series = row
    if data.get("delete") == "series" and series:  # эта и все следующие
        db.execute("DELETE FROM tasks WHERE series = ? AND user_id = ? AND start >= ?", (series, uid, start))
        db.commit()
        return web.json_response({"ok": True})
    if data.get("delete"):
        db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid))
        db.commit()
        return web.json_response({"ok": True})
    fields = fields_or_400(data)
    if data.get("confirm"):
        fields["status"] = "ok"
    if series and "repeat" in fields:
        if not fields.pop("repeat"):  # у серии повтор не меняется — его можно только остановить
            db.execute("DELETE FROM tasks WHERE series = ? AND user_id = ? AND start > ?", (series, uid, start))
            db.execute("UPDATE tasks SET series = NULL, repeat = '' WHERE id = ?", (task_id,))
            db.commit()
    update_task(task_id, uid, fields)
    start_series(task_id)
    return web.json_response({"ok": True})


async def api_settings(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    data = await json_body(request)
    fields = {}
    if "reminders" in data:
        if (reminders := clean_offsets(data["reminders"])) is None:
            raise web.HTTPBadRequest()
        fields["reminders"] = reminders
    if "smart_reminders" in data:
        fields["smart_reminders"] = int(bool(data["smart_reminders"]))
    for key in ("digest_hour", "evening_hour"):
        if key in data:
            hour = data[key]
            if hour is not None and not (type(hour) is int and 0 <= hour <= 23):
                raise web.HTTPBadRequest()
            fields[key] = hour
    register(uid)
    if fields:  # ключи только из списка выше
        db.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), uid))
        db.commit()
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(Path(__file__).with_name("webapp.html"))


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/tasks", api_tasks),
        web.post("/api/tasks", api_create),
        web.post(r"/api/tasks/{id:\d+}", api_update),
        web.post("/api/settings", api_settings),
        web.get("/cal/{name}", feed),
        web.get("/subscribe/cal/{name}", subscribe),
    ])
    return app


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(os.environ["BOT_TOKEN"])
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await bot.set_my_commands(COMMANDS)
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    await bot.set_my_description(DESCRIPTION)
    await bot.set_my_short_description(SHORT_DESCRIPTION)
    if PUBLIC_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Календарь", web_app=WebAppInfo(url=PUBLIC_URL)))
    reminders = asyncio.create_task(scheduler(bot))  # noqa: F841 — держим ссылку, чтобы задачу не собрал GC
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
