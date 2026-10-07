import asyncio
import calendar as cal_lib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
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
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
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

import texts
from texts import CATEGORIES, CATEGORY_ICONS, LANGS, PRIORITIES, t

if Path(".env").exists():
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Bishkek"))
# Бесплатный лимит Gemini считается отдельно на каждую модель — при отказе пробуем следующую.
# Основная — самая быстрая из проверенных (~1–2 с на ответ); более тяжёлые «думают» 10–25 с.
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
          *filter(None, os.getenv("GEMINI_FALLBACKS", "gemini-3.1-flash-lite,gemini-flash-latest").split(","))]
LLM_TIMEOUT_MS = 20_000  # зависшая модель не должна держать ответ — переходим к следующей
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")  # HTTPS-адрес сервера (туннель или хостинг)
PORT = int(os.getenv("PORT", "8080"))
ALL_DAY_HOUR = 9  # от этого часа считаются напоминания для задач «на весь день»
MAX_OFFSET = 30 * 24 * 60  # самое раннее напоминание — за 30 дней
MAX_REMINDERS = 10
MAX_SUBTASKS = 20
SNOOZE_MINUTES = (15, 60)
SERIES_HORIZON = timedelta(days=60)  # на сколько вперёд создаются повторы
EVENT_LENGTH = timedelta(hours=1)  # считаем, что у задачи со временем длительность час
BATCH_DELAY = 2.5  # столько секунд ждём следующих пересылок, прежде чем отдать пачку ИИ одним запросом
PLAN_HOURS = (9, 21)  # в эти часы ИИ раскладывает дела без времени
WEEKLY_HOUR = 18  # воскресный обзор недели
REPEATS = ("", "daily", "weekdays", "weekly", "monthly")
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
GROUP = F.chat.type.in_({"group", "supergroup"})

# Дешёвый фильтр для групп: в LLM идут только сообщения с намёком на дату или дело.
HINT = re.compile(
    r"\d{1,2}[:.]\d{2}|\d{1,2}\s*(?:янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)"
    r"|сегодня|завтра|понедельник|вторник|сред[ау]|четверг|пятниц|суббот|воскресен"
    r"|встреч|созвон|митап|дедлайн|забрать|забери|не забудь|перенес|перенос|отмен"
    r"|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|meeting|call|deadline"
    r"|бүгүн|эртең|жолугуш|дүйшөмбү|шейшемби|шаршемби|бейшемби|жума|ишемби|жекшемби",
    re.I,
)

DIRECT_MODE = "Пользователь пишет тебе сам: это может быть новое дело, просьба изменить задачи или вопрос о расписании."
FORWARD_MODE = "Пользователь переслал тебе сообщение из другого чата."
BATCH_MODE = ("Пользователь переслал тебе подряд несколько сообщений одной переписки (формат: [время] отправитель: текст). "
              "Пойми, о чём в итоге договорились: поздние сообщения уточняют и отменяют ранние. Не создавай дубликатов.")

PROMPT = """Ты — ассистент-календарь в Telegram. Сейчас {now} ({weekday}). Поле reply пиши на языке: {lang}.
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
   - category: work (работа, созвоны, проекты), personal (друзья, семья, отдых), shopping (купить, забрать заказ),
     health (врач, спорт, лекарства), study (учёба, курсы, экзамены) или other;
   - priority: high — срочно или важно (дедлайн, врач, самолёт, «важно»), low — необязательно («если успею»), иначе normal;
   - subtasks: пункты, если в деле перечислены шаги или покупки («купить торт, шарики, свечи» → ["торт", "шарики", "свечи"]),
     иначе [];
   - reminders: за сколько минут до начала напомнить, по типу дела: самолёт/поезд — [1440, 180];
     встреча/созвон — [60, 10]; врач, важная встреча — [1440, 60]; дедлайн — [1440, 180];
     «забрать/купить» без времени — [0]; не уверен — [];
   - repeat: "daily", "weekdays", "weekly" или "monthly", если сказано «каждый день/по будням/каждую неделю/каждый месяц»
     или «каждый вторник» и т.п. (тогда start — ближайшее такое время), иначе "".
2. Сообщение про дело, которое уже есть в списке (перенос, отмена, новые детали), или просьба изменить/удалить/отметить
   задачу → update с id этой задачи. В title/start/location/description — только НОВЫЕ значения, остальное "".
   Отмена или «удали» → delete=true. «Сделал», «отметь выполненным» → done=true. Не создавай дубликат существующей задачи.
3. Вопрос о расписании («что у меня завтра», «когда я свободен в четверг», «что я обещал Мише») → ответ в reply:
   коротко, простым текстом без markdown, только по списку задач. На вопрос о свободном времени называй свободные окна.
4. Если непонятно, какую задачу менять, — спроси в reply и ничего не меняй.
5. Сам ничего не меняешь: create/update пользователь подтверждает кнопкой, поэтому не пиши в reply «удалил», «перенёс».
Пустые create/update/reply — если ничего из этого нет. Ничего не выдумывай.

Сообщение:
{text}"""

PLAN_PROMPT = """Ты планируешь день {day} ({weekday}).
Встречи со временем — их не двигать, каждая длится 60 минут:
{fixed}
Дела без времени — их нужно расставить (id | название | приоритет | категория | описание):
{free}
Поставь каждое дело без времени в свободное окно между {start}:00 и {end}:00{after}. Оцени длительность в минутах (minutes),
важное (high) ставь раньше, между делами оставляй 10–15 минут, не пересекайся со встречами и друг с другом.
time — "HH:MM", кратно 5 минутам. comment — одна короткая дружелюбная фраза о плане на языке: {lang}."""

WEEK_PROMPT = """Ты — ассистент-календарь. Задачи пользователя на ближайшие 7 дней (дата | время | название | категория | приоритет):
{tasks}
Напиши короткий обзор этих дней на языке: {lang}. 2–4 пункта, каждый с новой строки и начинается с «• ».
Отметь самые загруженные дни и важные дела, дай 1–2 конкретных совета (что перенести, где оставить время на отдых).
Простой текст без markdown."""

db = sqlite3.connect(os.getenv("DB_PATH", "tasks.db"))
db.create_function("py_lower", 1, lambda s: s.lower() if isinstance(s, str) else s, deterministic=True)  # LIKE в SQLite не понимает регистр кириллицы
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
CREATE TABLE IF NOT EXISTS changes (  -- предложенные ИИ правки и планы дня, ждут подтверждения
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    fields TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trash (  -- удалённое из Mini App, чтобы можно было «Отменить»
    batch TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    row TEXT NOT NULL,
    at TEXT NOT NULL
);
""")
for table, column in [
    ("tasks", "done INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "reminders TEXT NOT NULL DEFAULT '60'"),  # минуты до начала через запятую: "1440,60"
    ("tasks", "sent TEXT NOT NULL DEFAULT ''"),  # какие из них уже отправлены
    ("tasks", "snooze TEXT"),  # «напомнить позже»: когда повторить
    ("tasks", "description TEXT NOT NULL DEFAULT ''"),
    ("tasks", "repeat TEXT NOT NULL DEFAULT ''"),  # одно из REPEATS
    ("tasks", "series INTEGER"),  # id первой задачи серии повторов
    ("tasks", "chat_id INTEGER"),  # группа, из которой задача пришла
    ("tasks", "category TEXT NOT NULL DEFAULT 'other'"),  # одно из CATEGORIES
    ("tasks", "priority TEXT NOT NULL DEFAULT 'normal'"),  # одно из PRIORITIES
    ("tasks", "subtasks TEXT NOT NULL DEFAULT '[]'"),  # JSON: [{"text": ..., "done": false}]
    ("tasks", "origin TEXT NOT NULL DEFAULT 'ai'"),  # ai — нашёл ИИ, manual — вручную, shared — от собеседника
    ("users", "reminders TEXT NOT NULL DEFAULT '60'"),  # набор по умолчанию для новых задач
    ("users", "smart_reminders INTEGER NOT NULL DEFAULT 1"),  # ИИ подбирает напоминания под тип дела
    ("users", "digest_hour INTEGER DEFAULT 9"),  # час утреннего плана, NULL — выключен
    ("users", "digest_sent TEXT"),
    ("users", "evening_hour INTEGER DEFAULT 21"),  # час вечернего итога, NULL — выключен (и обзор недели тоже)
    ("users", "evening_sent TEXT"),
    ("users", "weekly_sent TEXT"),
    ("users", "lang TEXT"),  # одно из LANGS
]:
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
    except sqlite3.OperationalError:  # колонка уже есть
        pass

TASK_COLUMNS = {"title", "start", "location", "description", "reminders", "sent", "snooze", "done", "repeat", "status",
                "category", "priority", "subtasks"}
CARD_COLUMNS = ("title", "start", "location", "source", "description", "category", "priority", "subtasks", "reminders", "repeat")

dp = Dispatcher()
BOT: Bot | None = None  # нужен веб-обработчикам, чтобы обновить кнопку меню при смене языка
batches: dict[int, list[Message]] = {}  # пересылки, которые ждут отправки в ИИ пачкой
batch_timers: dict[int, asyncio.Task] = {}


class NewTask(BaseModel):
    title: str
    start: str
    location: str
    description: str
    category: str
    priority: str
    subtasks: list[str]
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


class PlanItem(BaseModel):
    id: int
    time: str
    minutes: int


class Plan(BaseModel):
    items: list[PlanItem]
    comment: str


@cache
def llm() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"timeout": LLM_TIMEOUT_MS})


# ── Даты ─────────────────────────────────────────────────────────

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


def human(lang: str, start: str) -> str:
    d = datetime.fromisoformat(start)
    day = texts.day_name(lang, d.date(), datetime.now(TZ).date())
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


def clean_subtasks(value) -> str | None:
    """Подзадачи: строки или {"text", "done"}; None — мусор."""
    if not isinstance(value, list) or len(value) > MAX_SUBTASKS:
        return None
    items = []
    for item in value:
        if isinstance(item, str):
            item = {"text": item}
        if not isinstance(item, dict) or not (text := str(item.get("text", "")).strip()[:100]):
            return None
        items.append({"text": text, "done": bool(item.get("done"))})
    return json.dumps(items, ensure_ascii=False)


def event_time(start: str) -> datetime:
    d = datetime.fromisoformat(start)
    return (d if "T" in start else d.replace(hour=ALL_DAY_HOUR)).replace(tzinfo=TZ)


def due_offsets(start: str, offsets: list[int], sent: set[int], now: datetime) -> list[int]:
    """Напоминания, время которых пришло. После начала события (с запасом 2 мин) — уже ничего."""
    t_ = event_time(start)
    if now > t_ + timedelta(minutes=2):
        return []
    return [m for m in offsets if m not in sent and now >= t_ - timedelta(minutes=m)]


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


def like(q: str) -> str:
    """Шаблон для LIKE ... ESCAPE '\\' из пользовательского текста."""
    return "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# ── Внешние календари ────────────────────────────────────────────

def gcal_link(title: str, start: str, location: str, description: str = "") -> str:
    s = datetime.fromisoformat(start)
    if "T" in start:
        dates = f"{s:%Y%m%dT%H%M%S}/{s + EVENT_LENGTH:%Y%m%dT%H%M%S}"
    else:
        dates = f"{s:%Y%m%d}/{s + timedelta(days=1):%Y%m%d}"
    query = {"action": "TEMPLATE", "text": title, "dates": dates, "location": location, "details": description, "ctz": TZ.key}
    return "https://calendar.google.com/calendar/render?" + urlencode(query)


def sign(payload: str, length: int) -> str:
    return hmac.new(os.environ["BOT_TOKEN"].encode(), payload.encode(), hashlib.sha256).hexdigest()[:length]


def cal_path(uid: int) -> str:
    # ponytail: подпись от BOT_TOKEN — после /revoke токена старые ссылки на календарь перестанут работать.
    return f"cal/{uid}-{sign(str(uid), 32)}.ics"


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


def ics(rows: list[tuple[int, str, str, str, str]], name: str = "Задачи из Telegram") -> str:
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Task Tracker//RU", f"X-WR-CALNAME:{ics_escape(name)}",
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


# ── Пользователи и задачи в базе ─────────────────────────────────

def register(uid: int, code: str | None = None) -> str:
    """Запоминает пользователя; язык — из Telegram, пока он не выбрал свой в настройках. Возвращает язык."""
    db.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (uid,))
    db.execute("UPDATE users SET lang = ? WHERE id = ? AND lang IS NULL", (texts.lang_from_code(code), uid))
    db.commit()
    return lang_of(uid)


def lang_of(uid: int) -> str:
    row = db.execute("SELECT lang FROM users WHERE id = ?", (uid,)).fetchone()
    return row[0] if row and row[0] in LANGS else "ru"


def is_user(uid: int) -> bool:
    return bool(db.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone())


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
    for key, allowed in (("repeat", REPEATS), ("category", CATEGORIES), ("priority", PRIORITIES)):
        if key in data:
            if data[key] not in allowed:
                return None
            fields[key] = data[key]
    if "subtasks" in data:
        if (subtasks := clean_subtasks(data["subtasks"])) is None:
            return None
        fields["subtasks"] = subtasks
    return fields


def update_task(task_id: int, uid: int, fields: dict) -> None:
    assert set(fields) <= TASK_COLUMNS  # имена колонок подставляются в SQL — только из белого списка
    if fields:
        db.execute(
            f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ? AND user_id = ?",
            (*fields.values(), task_id, uid),
        )
        db.commit()


def task_dict(task_id: int) -> dict | None:
    row = db.execute(f"SELECT {', '.join(CARD_COLUMNS)} FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(zip(CARD_COLUMNS, row)) if row else None


def fresh_subtasks(subtasks: str) -> str:
    """Те же пункты, но не отмеченные — для повторов и копий у собеседника."""
    return json.dumps([{**s, "done": False} for s in json.loads(subtasks or "[]")], ensure_ascii=False)


def extend_series(series: int, until: datetime) -> None:
    """Достраивает повторы серии до даты until, копируя последнюю задачу серии."""
    row = db.execute(
        "SELECT user_id, title, start, location, description, source, chat_id, reminders, repeat, category, priority,"
        " subtasks, origin FROM tasks WHERE series = ? ORDER BY start DESC LIMIT 1",
        (series,),
    ).fetchone()
    if not row or not row[8] or row[8] not in REPEATS:
        return
    uid, title, start, location, description, source, chat_id, reminders, repeat, category, priority, subtasks, origin = row
    d = datetime.fromisoformat(start)
    while (d := next_occurrence(d, repeat)) <= until.replace(tzinfo=None):
        db.execute(
            "INSERT INTO tasks (user_id, title, start, location, description, source, chat_id, reminders, repeat, series,"
            " status, category, priority, subtasks, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)",
            (uid, title, fmt_like(d, start), location, description, source, chat_id, reminders, repeat, series,
             category, priority, fresh_subtasks(subtasks), origin),
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
    t_ = datetime.fromisoformat(start)
    return db.execute(
        "SELECT title, start FROM tasks WHERE user_id = ? AND id != ? AND status = 'ok' AND done = 0"
        " AND start LIKE '%T%' AND start > ? AND start < ? ORDER BY start",
        (uid, exclude, fmt_like(t_ - EVENT_LENGTH, start), fmt_like(t_ + EVENT_LENGTH, start)),
    ).fetchall()


def trash_tasks(uid: int, where: str, params: tuple) -> str:
    """Удаляет задачи, сохранив копии для «Отменить». where — только строки из этого файла."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(tasks)")]
    rows = db.execute(f"SELECT {', '.join(cols)} FROM tasks WHERE user_id = ? AND {where}", (uid, *params)).fetchall()
    batch, now = secrets.token_hex(8), f"{datetime.now(TZ):%Y-%m-%dT%H:%M}"
    db.execute("DELETE FROM trash WHERE user_id = ? AND at < ?", (uid, f"{datetime.now(TZ) - timedelta(days=1):%Y-%m-%dT%H:%M}"))
    db.executemany("INSERT INTO trash (batch, user_id, row, at) VALUES (?, ?, ?, ?)",
                   [(batch, uid, json.dumps(dict(zip(cols, r)), ensure_ascii=False), now) for r in rows])
    db.execute(f"DELETE FROM tasks WHERE user_id = ? AND {where}", (uid, *params))
    db.commit()
    return batch


def restore_tasks(uid: int, batch: str) -> int:
    cols = {r[1] for r in db.execute("PRAGMA table_info(tasks)")}
    rows = db.execute("SELECT row FROM trash WHERE batch = ? AND user_id = ?", (batch, uid)).fetchall()
    for (row,) in rows:
        data = {k: v for k, v in json.loads(row).items() if k in cols}  # имена колонок — из самой базы
        db.execute(f"INSERT OR IGNORE INTO tasks ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
                   tuple(data.values()))
    db.execute("DELETE FROM trash WHERE batch = ? AND user_id = ?", (batch, uid))
    db.commit()
    return len(rows)


# ── Тексты и кнопки ──────────────────────────────────────────────

def card(lang: str, task: dict) -> str:
    """HTML-карточка задачи; всё пользовательское экранируется."""
    icon = CATEGORY_ICONS.get(task.get("category"), "📌")
    lines = [f"{icon} <b>{escape(task['title'])}</b>", f"🗓 {human(lang, task['start'])}"]
    if task.get("priority") == "high":
        lines[0] += " 🔥"
    if task.get("location"):
        lines.append(f"📍 {escape(task['location'])}")
    if task.get("description"):
        lines.append(f"📝 {escape(task['description'])}")
    lines += [f"{'☑️' if s['done'] else '▫️'} {escape(s['text'])}" for s in json.loads(task.get("subtasks") or "[]")]
    if task.get("source"):
        lines.append(f"💬 {escape(task['source'])}")
    return "\n".join(lines)


def extras(lang: str, uid: int, task_id: int, task: dict) -> str:
    """Строки под карточкой: напоминания, повтор, пересечения."""
    offsets = parse_offsets(task["reminders"])
    lines = [t(lang, "remind", list=", ".join(texts.offset_label(lang, m) for m in offsets)) if offsets else t(lang, "no_remind")]
    if task["repeat"]:
        lines.append(t(lang, "repeat", label=t(lang, f"rep_{task['repeat']}")))
    if clash := conflicts(uid, task["start"], task_id):
        lines.append(t(lang, "conflict", list=", ".join(f"{s[11:16]} {escape(x)}" for x, s in clash)))
    return "\n".join(lines)


def task_text(lang: str, uid: int, task_id: int) -> str:
    task = task_dict(task_id)
    return card(lang, task) + "\n" + extras(lang, uid, task_id, task)


def app_button(lang: str, key: str = "btn_open", **query) -> list[InlineKeyboardButton]:
    """Кнопка Mini App; query открывает нужный экран: task+date — задачу, view=settings — настройки."""
    if not PUBLIC_URL:
        return []
    url = f"{PUBLIC_URL}/?{urlencode(query)}" if query else PUBLIC_URL
    return [InlineKeyboardButton(text=t(lang, key), web_app=WebAppInfo(url=url))]


def keyboard(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    """Клавиатура без пустых рядов (Telegram их не принимает)."""
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def set_menu(bot: Bot | None, uid: int, lang: str) -> None:
    """Кнопка «Календарь» у поля ввода — на языке пользователя."""
    if bot and PUBLIC_URL:
        try:
            await bot.set_chat_menu_button(chat_id=uid, menu_button=MenuButtonWebApp(
                text=t(lang, "menu"), web_app=WebAppInfo(url=PUBLIC_URL)))
        except TelegramAPIError as e:
            logging.info("menu %s: %s", uid, e)


def start_button(lang: str, bot_username: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=t(lang, "btn_get_tasks"), url=f"https://t.me/{bot_username}?start=group")]


# ── ИИ ───────────────────────────────────────────────────────────

async def ask_model(contents: list, schema=None):
    """Запрос к Gemini с перебором моделей; schema=None — ответ простым текстом."""
    config = {"response_mime_type": "application/json", "response_schema": schema} if schema else None
    for attempt in range(2):
        for model in MODELS:
            try:
                resp = await llm().aio.models.generate_content(model=model, contents=contents, config=config)
                return resp.parsed if schema else resp.text
            except errors.APIError as e:
                if e.code not in (404, 429, 500, 503):  # нет модели, лимит или перегрузка — пробуем дальше
                    raise
                logging.warning("gemini %s: %s", model, e.code)
            except TimeoutError:
                logging.warning("gemini %s: timeout", model)
        await asyncio.sleep(5)
    raise RuntimeError("все модели Gemini недоступны")


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


async def message_input(m: Message) -> tuple[str, datetime, list]:
    """Текст, момент отправки (для «завтра») и вложения сообщения."""
    ref = (m.forward_origin.date if m.forward_origin else m.date).astimezone(TZ)
    attached = "голосовое" if m.voice else "изображение"
    return m.text or m.caption or f"(текста нет — {attached} во вложении)", ref, await media_parts(m)


def origin_name(m: Message) -> str:
    """Кто написал пересланное сообщение."""
    o = m.forward_origin
    for attr in ("sender_user", "sender_chat", "chat"):
        if x := getattr(o, attr, None):
            return getattr(x, "first_name", None) or getattr(x, "title", None) or "?"
    return getattr(o, "sender_user_name", None) or "?"


async def analyze(text: str, ref: datetime, parts: list, mode: str, tasks: str, lang: str) -> Result | None:
    """Один запрос к Gemini: что сделать с сообщением. None — ошибка."""
    now = datetime.now(TZ)
    prompt = PROMPT.format(
        now=f"{now:%Y-%m-%d %H:%M}", weekday=WEEKDAYS[now.weekday()], lang=texts.LANG_NAMES[lang], mode=mode,
        tasks=tasks, ref=f"{ref:%Y-%m-%d %H:%M} ({WEEKDAYS[ref.weekday()]})", text=text,
    )
    try:
        return await ask_model([prompt, *parts], Result)
    except Exception as e:  # сеть, неверный ключ, все модели недоступны
        logging.warning("gemini: %s", e)
        return None


def change_data(c: Change) -> dict:
    if c.delete:
        return {"delete": True}
    data = {k: v for k, v in (("title", c.title), ("start", c.start), ("location", c.location),
                              ("description", c.description)) if v}
    return data | ({"done": True} if c.done else {})


async def create_pending(bot: Bot, uid: int, lang: str, nt: NewTask, source: str, chat_id: int | None) -> bool:
    if not (start := normalize(nt.start)) or not nt.title.strip():
        return False
    smart, default = db.execute("SELECT smart_reminders, reminders FROM users WHERE id = ?", (uid,)).fetchone() or (1, "60")
    reminders = (smart and nt.reminders and clean_offsets(nt.reminders)) or default
    subtasks = clean_subtasks([s for s in nt.subtasks if s.strip()][:MAX_SUBTASKS]) or "[]"
    task_id = db.execute(
        "INSERT INTO tasks (user_id, title, start, location, description, source, chat_id, reminders, repeat, category,"
        " priority, subtasks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, nt.title.strip()[:100], start, nt.location[:100], nt.description[:500], source, chat_id, reminders,
         nt.repeat if nt.repeat in REPEATS else "", nt.category if nt.category in CATEGORIES else "other",
         nt.priority if nt.priority in PRIORITIES else "normal", subtasks),
    ).lastrowid
    db.commit()
    kb = keyboard([
        InlineKeyboardButton(text=t(lang, "btn_add"), callback_data=f"ok:{task_id}"),
        InlineKeyboardButton(text="✕", callback_data=f"no:{task_id}"),
    ])
    await bot.send_message(uid, task_text(lang, uid, task_id), parse_mode="HTML", reply_markup=kb)
    return True


def change_text(lang: str, uid: int, task_id: int, data: dict) -> str | None:
    """Описание правки «было → стало»; None — менять нечего."""
    task = task_dict(task_id)
    if data.get("delete"):
        return t(lang, "ch_delete") + "\n\n" + card(lang, task)
    done = db.execute("SELECT done FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
    lines = []
    if data.get("title", task["title"]) != task["title"]:
        lines.append(f"📌 {escape(task['title'])} → <b>{escape(data['title'])}</b>")
    if data.get("start", task["start"]) != task["start"]:
        lines.append(f"🗓 {human(lang, task['start'])} → <b>{human(lang, data['start'])}</b>")
        if clash := conflicts(uid, data["start"], task_id):
            lines.append(t(lang, "conflict", list=", ".join(f"{s[11:16]} {escape(x)}" for x, s in clash)))
    if data.get("location", task["location"]) != task["location"]:
        lines.append(f"📍 {escape(task['location'] or '—')} → <b>{escape(data['location'])}</b>")
    if data.get("description", task["description"]) != task["description"]:
        lines.append(f"📝 <b>{escape(data['description'])}</b>")
    if data.get("done") and not done:
        lines.append(t(lang, "ch_done"))
    return t(lang, "ch_edit", title=escape(task["title"])) + "\n\n" + "\n".join(lines) if lines else None


async def propose_change(bot: Bot, uid: int, lang: str, task_id: int, data: dict) -> bool:
    """Правка от ИИ не применяется сразу — пользователь подтверждает её кнопкой."""
    if not db.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone():
        return False  # ИИ ошибся с id или это чужая задача
    if not data.get("delete") and not (data := clean_fields(data)):
        return False
    if not (text := change_text(lang, uid, task_id, data)):
        return False
    await send_proposal(bot, uid, lang, task_id, data, text)
    return True


async def send_proposal(bot: Bot, uid: int, lang: str, task_id: int, data: dict, text: str) -> None:
    change_id = db.execute(
        "INSERT INTO changes (user_id, task_id, fields) VALUES (?, ?, ?)", (uid, task_id, json.dumps(data))
    ).lastrowid
    db.commit()
    kb = keyboard([
        InlineKeyboardButton(text=t(lang, "btn_apply"), callback_data=f"ch:ok:{change_id}"),
        InlineKeyboardButton(text="✕", callback_data=f"ch:no:{change_id}"),
    ])
    await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)


async def respond(bot: Bot, chat_id: int, uid: int, text: str, ref: datetime, parts: list, mode: str) -> None:
    """Сообщение (или пачка) → ИИ → карточки задач, предложения правок или ответ на вопрос."""
    lang = lang_of(uid)
    await bot.send_chat_action(chat_id, "typing")
    if (res := await analyze(text, ref, parts, mode, user_context(uid), lang)) is None:
        await bot.send_message(chat_id, t(lang, "ai_failed"))
        return
    acted = 0
    for nt in res.create:
        acted += await create_pending(bot, uid, lang, nt, "", None)
    for c in res.update:
        acted += await propose_change(bot, uid, lang, c.id, change_data(c))
    if res.reply and not acted:  # при правках ответ ИИ не нужен — всё видно в карточках
        await bot.send_message(chat_id, res.reply)
    elif not acted:
        await bot.send_message(chat_id, t(lang, "nothing_found"))


async def flush_batch(bot: Bot, chat_id: int, uid: int) -> None:
    """Через BATCH_DELAY после последней пересылки — вся пачка одним запросом к ИИ."""
    await asyncio.sleep(BATCH_DELAY)
    batch_timers.pop(uid, None)
    msgs = batches.pop(uid, [])
    if not msgs:
        return
    try:
        if len(msgs) == 1:
            text, mode = msgs[0].text or msgs[0].caption, FORWARD_MODE
        else:
            text = "\n".join(f"[{x.forward_origin.date.astimezone(TZ):%Y-%m-%d %H:%M}] {origin_name(x)}: {x.text or x.caption}"
                             for x in msgs)
            mode = BATCH_MODE
        await respond(bot, chat_id, uid, text, msgs[-1].forward_origin.date.astimezone(TZ), [], mode)
    except Exception:
        logging.exception("batch")


# ── Планирование дня и обзор недели ──────────────────────────────

async def make_plan(uid: int, day: str, lang: str) -> tuple[list[dict], str] | None:
    """ИИ расставляет дела без времени по свободным окнам. None — ошибка ИИ, [] — расставлять нечего."""
    rows = db.execute(
        "SELECT id, title, start, priority, category, description FROM tasks"
        " WHERE user_id = ? AND status = 'ok' AND done = 0 AND substr(start, 1, 10) = ? ORDER BY start",
        (uid, day),
    ).fetchall()
    free = {r[0]: r for r in rows if "T" not in r[2]}
    if not free:
        return [], ""
    now = datetime.now(TZ)
    d = date.fromisoformat(day)
    prompt = PLAN_PROMPT.format(
        day=day, weekday=WEEKDAYS[d.weekday()], lang=texts.LANG_NAMES[lang],
        fixed="\n".join(f"{r[2][11:16]} {r[1]}" for r in rows if "T" in r[2]) or "(нет)",
        free="\n".join(f"{r[0]} | {r[1]} | {r[3]} | {r[4]} | {r[5] or '-'}" for r in free.values()),
        start=PLAN_HOURS[0], end=PLAN_HOURS[1], after=f", не раньше {now:%H:%M}" if d == now.date() else "",
    )
    try:
        plan = await ask_model([prompt], Plan)
    except Exception as e:
        logging.warning("plan: %s", e)
        return None
    items = {}
    for it in plan.items:
        if it.id in free and it.id not in items and re.fullmatch(r"\d{2}:\d{2}", it.time) and normalize(f"{day}T{it.time}"):
            items[it.id] = {"id": it.id, "title": free[it.id][1], "start": f"{day}T{it.time}",
                            "minutes": max(5, min(it.minutes, 600))}
    return sorted(items.values(), key=lambda i: i["start"]), plan.comment


def plan_text(lang: str, day: str, items: list[dict], comment: str) -> str:
    lines = [t(lang, "plan_header", day=texts.day_name(lang, date.fromisoformat(day), datetime.now(TZ).date())), ""]
    for i in items:
        start = datetime.fromisoformat(i["start"])
        lines.append(f"{start:%H:%M}–{start + timedelta(minutes=i['minutes']):%H:%M}  {escape(i['title'])}")
    return "\n".join(lines) + (f"\n\n{escape(comment)}" if comment else "")


async def send_plan(bot: Bot, chat_id: int, uid: int, day: str) -> None:
    lang = lang_of(uid)
    await bot.send_chat_action(chat_id, "typing")
    if (result := await make_plan(uid, day, lang)) is None:
        await bot.send_message(chat_id, t(lang, "plan_failed"))
        return
    items, comment = result
    if not items:
        await bot.send_message(chat_id, t(lang, "plan_nothing"))
        return
    await send_proposal(bot, uid, lang, 0, {"plan": [[i["id"], i["start"]] for i in items]},
                        plan_text(lang, day, items, comment))


def apply_plan(uid: int, plan: list) -> int:
    applied = 0
    for task_id, start in plan:
        if (fields := clean_fields({"start": start})) and \
                db.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone():
            update_task(task_id, uid, fields)
            applied += 1
    return applied


def week_stats(uid: int, monday: date) -> dict:
    rows = db.execute(
        "SELECT category, done, origin FROM tasks WHERE user_id = ? AND status = 'ok' AND start >= ? AND start < ?",
        (uid, f"{monday}", f"{monday + timedelta(days=7)}"),
    ).fetchall()
    by_category = {}
    for category, done, _ in rows:
        stat = by_category.setdefault(category, [0, 0])
        stat[0] += done
        stat[1] += 1
    return {"total": len(rows), "done": sum(r[1] for r in rows), "ai": sum(r[2] == "ai" for r in rows),
            "by_category": by_category}


async def week_text(uid: int, lang: str, today: date) -> str:
    """Продуктивность текущей недели + обзор следующих 7 дней от ИИ."""
    monday = today - timedelta(days=today.weekday())
    s = week_stats(uid, monday)
    lines = [t(lang, "week_header", range=f"{monday:%d.%m}–{monday + timedelta(days=6):%d.%m}")]
    if s["total"]:
        lines.append(t(lang, "week_done", done=s["done"], total=s["total"], pct=round(100 * s["done"] / s["total"])))
        if s["ai"]:
            lines.append(t(lang, "week_ai", items=texts.word(lang, "item", s["ai"])))
        lines.append(" · ".join(f"{CATEGORY_ICONS.get(c, '📌')} {t(lang, f'cat_{c}')} {d}/{n}"
                                for c, (d, n) in sorted(s["by_category"].items(), key=lambda kv: -kv[1][1])))
    else:
        lines.append(t(lang, "week_empty"))
    tomorrow = today + timedelta(days=1)
    upcoming = db.execute(
        "SELECT start, title, category, priority FROM tasks WHERE user_id = ? AND status = 'ok' AND done = 0"
        " AND start >= ? AND start < ? ORDER BY start",
        (uid, f"{tomorrow}", f"{tomorrow + timedelta(days=7)}"),
    ).fetchall()
    lines += ["", t(lang, "week_next")]
    if not upcoming:
        lines.append(t(lang, "week_next_empty"))
        return "\n".join(lines)
    listing = "\n".join(f"{s_[:10]} | {s_[11:16] or 'весь день'} | {title} | {cat} | {prio}" for s_, title, cat, prio in upcoming)
    try:
        overview = (await ask_model([WEEK_PROMPT.format(tasks=listing, lang=texts.LANG_NAMES[lang])])).strip()
    except Exception as e:
        logging.warning("week: %s", e)
        overview = None
    if overview:
        lines.append(escape(overview))
    else:  # без ИИ — просто сколько дел по дням
        per_day = {}
        for s_, *_ in upcoming:
            per_day[s_[:10]] = per_day.get(s_[:10], 0) + 1
        lines += [f"• {texts.day_name(lang, date.fromisoformat(d), today)} — {texts.word(lang, 'item', n)}" for d, n in per_day.items()]
    return "\n".join(lines)


# ── Команды и сообщения в личке ──────────────────────────────────

@dp.message(Command("start", "help"), F.chat.type == "private")
async def start(m: Message):
    lang = register(m.from_user.id, m.from_user.language_code)
    me = await m.bot.me()
    await set_menu(m.bot, m.from_user.id, lang)
    name = escape(m.from_user.first_name or "")
    await m.answer(t(lang, "help", name=f", {name}" if name else "", bot=me.username), parse_mode="HTML",
                   reply_markup=keyboard(app_button(lang)))


@dp.message(Command("settings"), F.chat.type == "private")
async def settings(m: Message):
    lang = register(m.from_user.id, m.from_user.language_code)
    if not PUBLIC_URL:
        await m.answer(t(lang, "no_public_url"))
        return
    await m.answer(t(lang, "settings_text"), reply_markup=keyboard(app_button(lang, "btn_settings", view="settings")))


@dp.message(Command("calendar"), F.chat.type == "private")
async def calendar(m: Message):
    lang = register(m.from_user.id, m.from_user.language_code)
    if not PUBLIC_URL:
        await m.answer(t(lang, "no_public_url"))
        return
    links = cal_links(m.from_user.id)
    await m.answer(t(lang, "calendar_text", url=links["url"]), reply_markup=keyboard(
        [InlineKeyboardButton(text=t(lang, "btn_iphone"), url=links["apple"])],
        [InlineKeyboardButton(text=t(lang, "btn_google"), url=links["google"])],
    ))


async def agenda(m: Message, start_from: str, start_to: str, empty_key: str) -> None:
    lang = register(m.from_user.id, m.from_user.language_code)
    rows = db.execute(
        "SELECT title, start, category, priority FROM tasks WHERE user_id = ? AND status = 'ok' AND done = 0"
        " AND start >= ? AND start < ? ORDER BY start LIMIT 20",
        (m.from_user.id, start_from, start_to),
    ).fetchall()
    text = "\n".join(f"{CATEGORY_ICONS.get(c, '📌')} {human(lang, s)} — {escape(x)}{' 🔥' if p == 'high' else ''}"
                     for x, s, c, p in rows) or t(lang, empty_key)
    await m.answer(text, parse_mode="HTML", reply_markup=keyboard(app_button(lang)))


@dp.message(Command("today"), F.chat.type == "private")
async def today(m: Message):
    now = datetime.now(TZ)
    await agenda(m, f"{now:%Y-%m-%d}", f"{now + timedelta(days=1):%Y-%m-%d}", "today_empty")


@dp.message(Command("tasks"), F.chat.type == "private")
async def tasks(m: Message):
    await agenda(m, f"{datetime.now(TZ):%Y-%m-%d}", "9999", "tasks_empty")


@dp.message(Command("plan"), F.chat.type == "private")
async def plan(m: Message):
    register(m.from_user.id, m.from_user.language_code)
    await send_plan(m.bot, m.chat.id, m.from_user.id, f"{datetime.now(TZ):%Y-%m-%d}")


@dp.message(Command("week"), F.chat.type == "private")
async def week(m: Message):
    lang = register(m.from_user.id, m.from_user.language_code)
    await m.bot.send_chat_action(m.chat.id, "typing")
    text = await week_text(m.from_user.id, lang, datetime.now(TZ).date())
    await m.answer(text, parse_mode="HTML", reply_markup=keyboard(app_button(lang)))


@dp.message(F.chat.type == "private", F.text | F.caption | F.voice | F.photo | F.document)
async def private(m: Message):
    uid = m.from_user.id
    lang = register(uid, m.from_user.language_code)
    if (m.text or "").startswith("/"):  # неизвестная команда — не тратим запрос к ИИ
        await m.answer(t(lang, "unknown_cmd"))
        return
    if m.document and not (m.document.mime_type or "").startswith("image/") and not m.caption:
        await m.reply(t(lang, "doc_unsupported"))
        return
    if m.forward_origin and not (m.voice or m.photo or m.document):  # текстовые пересылки копим в пачку
        batches.setdefault(uid, []).append(m)
        if timer := batch_timers.get(uid):
            timer.cancel()
        batch_timers[uid] = asyncio.create_task(flush_batch(m.bot, m.chat.id, uid))
        return
    text, ref, parts = await message_input(m)
    await respond(m.bot, m.chat.id, uid, text, ref, parts, FORWARD_MODE if m.forward_origin else DIRECT_MODE)


# ── Группы ───────────────────────────────────────────────────────

@dp.message(Command("start", "help"), GROUP)
async def group_help(m: Message):
    lang = texts.lang_from_code(m.from_user.language_code)
    me = await m.bot.me()
    await m.reply(t(lang, "group_help"), reply_markup=keyboard(start_button(lang, me.username)))


@dp.message(Command("today", "tasks", "plan", "week", "settings", "calendar"), GROUP)
async def group_private_only(m: Message):
    lang = texts.lang_from_code(m.from_user.language_code)
    me = await m.bot.me()
    await m.reply(t(lang, "private_only"), reply_markup=keyboard(start_button(lang, me.username)))


@dp.message(Command("task"), GROUP)
async def group_task(m: Message):
    """/task ответом на сообщение: явная просьба разобрать его (в т.ч. голосовое или фото). Задача — только автору команды."""
    uid = m.from_user.id
    lang = texts.lang_from_code(m.from_user.language_code)
    me = await m.bot.me()
    if not (target := m.reply_to_message):
        await m.reply(t(lang, "task_need_reply"))
        return
    if not is_user(uid):
        await m.reply(t(lang, "start_first"), reply_markup=keyboard(start_button(lang, me.username)))
        return
    lang = lang_of(uid)
    mode = f"Пользователь попросил сделать задачу из сообщения в групповом чате «{m.chat.title or ''}»."
    text, ref, parts = await message_input(target.as_(m.bot))
    if (res := await analyze(text, ref, parts, mode, user_context(uid), lang)) is None:
        await m.reply(t(lang, "ai_failed"))
        return
    acted = 0
    for nt in res.create:
        acted += await create_pending(m.bot, uid, lang, nt, m.chat.title or "", m.chat.id)
    for c in res.update:
        acted += await propose_change(m.bot, uid, lang, c.id, change_data(c))
    if acted:
        await m.react([ReactionTypeEmoji(emoji="👍")])  # тихо: карточка уже в личке
    else:
        await m.reply(t(lang, "nothing_here"))


@dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION), GROUP)
async def added_to_group(event: ChatMemberUpdated):
    """Бота добавили в группу: объясняем, как он работает, и предупреждаем, если он не видит сообщений."""
    lang = texts.lang_from_code(event.from_user.language_code)
    me = await event.bot.get_me()
    text = t(lang, "group_welcome")
    if not me.can_read_all_group_messages and event.new_chat_member.status != "administrator":
        text += "\n\n" + t(lang, "group_privacy")
    await event.answer(text + "\n\n" + t(lang, "group_voice"), reply_markup=keyboard(start_button(lang, me.username)))


@dp.message(GROUP, F.text | F.caption)
async def group(m: Message):
    if not HINT.search(m.text or m.caption):
        return
    mode = f"Сообщение из группового чата «{m.chat.title or ''}». В списке — дела, уже найденные в этом чате."
    text, ref, _ = await message_input(m)
    if not (res := await analyze(text, ref, [], mode, group_context(m.chat.id), "ru")):
        return
    if res.create:
        # ponytail: перебор всех пользователей бота с проверкой членства — O(users) запросов; при росте хранить связку чат↔пользователь.
        for (uid,) in db.execute("SELECT id FROM users").fetchall():
            try:
                if (await m.bot.get_chat_member(m.chat.id, uid)).status in ("left", "kicked"):
                    continue
                for nt in res.create:
                    await create_pending(m.bot, uid, lang_of(uid), nt, m.chat.title or "", m.chat.id)
            except TelegramAPIError as e:  # пользователь не в чате или заблокировал бота
                logging.info("skip %s: %s", uid, e)
    for c in res.update:  # перенос/отмена — каждому, у кого есть копия этой задачи
        if not (rep := db.execute("SELECT title, start FROM tasks WHERE id = ? AND chat_id = ?", (c.id, m.chat.id)).fetchone()):
            continue
        for task_id, uid in db.execute(
            "SELECT id, user_id FROM tasks WHERE chat_id = ? AND title = ? AND start = ?", (m.chat.id, *rep)
        ).fetchall():
            try:
                await propose_change(m.bot, uid, lang_of(uid), task_id, change_data(c))
            except TelegramAPIError as e:
                logging.info("skip %s: %s", uid, e)


# ── Встроенный режим: поделиться задачей в любом чате ────────────

@dp.inline_query()
async def inline(q: InlineQuery):
    uid = q.from_user.id
    lang = lang_of(uid) if is_user(uid) else texts.lang_from_code(q.from_user.language_code)
    query = q.query.strip()
    rows = db.execute(
        "SELECT id FROM tasks WHERE user_id = ? AND status = 'ok' AND start >= ? AND (? = '' OR py_lower(title) LIKE ? ESCAPE '\\'"
        " OR py_lower(location) LIKE ? ESCAPE '\\' OR py_lower(description) LIKE ? ESCAPE '\\') ORDER BY start LIMIT 20",
        (uid, f"{datetime.now(TZ):%Y-%m-%d}", query, like(query), like(query), like(query)),
    ).fetchall()
    me = await q.bot.me()
    results = []
    for (task_id,) in rows:
        task = task_dict(task_id) | {"source": ""}
        results.append(InlineQueryResultArticle(
            id=str(task_id),
            title=f"{CATEGORY_ICONS.get(task['category'], '📌')} {task['title']}",
            description=human(lang, task["start"]) + (f" · {task['location']}" if task["location"] else ""),
            input_message_content=InputTextMessageContent(message_text=card(lang, task), parse_mode="HTML"),
            reply_markup=keyboard(
                [InlineKeyboardButton(text=t(lang, "inline_add"), callback_data=f"sh:{task_id}:{sign(f'share:{task_id}', 12)}")],
                [InlineKeyboardButton(text=t(lang, "inline_open"), url=f"https://t.me/{me.username}")],
            ),
        ))
    await q.answer(results, cache_time=0, is_personal=True,
                   button=None if results else InlineQueryResultsButton(text=t(lang, "inline_empty"), start_parameter="inline"))


@dp.callback_query(F.data.regexp(r"^sh:\d+:[0-9a-f]{12}$"))
async def share_add(c: CallbackQuery):
    """Собеседник нажал «Добавить себе» под карточкой из встроенного режима."""
    _, task_id, sig = c.data.split(":")
    uid = c.from_user.id
    lang = lang_of(uid) if is_user(uid) else texts.lang_from_code(c.from_user.language_code)
    if not hmac.compare_digest(sig, sign(f"share:{task_id}", 12)):
        await c.answer()
        return
    if not (task := task_dict(int(task_id))):
        await c.answer(t(lang, "inline_gone"), show_alert=True)
        return
    if not is_user(uid):
        await c.answer(t(lang, "inline_start"), show_alert=True)
        return
    if db.execute("SELECT 1 FROM tasks WHERE user_id = ? AND title = ? AND start = ?", (uid, task["title"], task["start"])).fetchone():
        await c.answer(t(lang, "inline_already"), show_alert=True)
        return
    db.execute(
        "INSERT INTO tasks (user_id, title, start, location, description, category, priority, subtasks, reminders, status,"
        " origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT reminders FROM users WHERE id = ?), 'ok', 'shared')",
        (uid, task["title"], task["start"], task["location"], task["description"], task["category"], task["priority"],
         fresh_subtasks(task["subtasks"]), uid),
    )
    db.commit()
    await c.answer(t(lang, "inline_added"), show_alert=True)


# ── Кнопки ───────────────────────────────────────────────────────

@dp.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def decide(c: CallbackQuery):
    action, task_id = c.data.split(":")
    uid, task_id = c.from_user.id, int(task_id)
    lang = lang_of(uid)
    if not db.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ? AND status = 'pending'", (task_id, uid)).fetchone():
        await c.answer(t(lang, "handled"))
        return
    if action == "no":
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        db.commit()
        await c.answer()
        await c.message.delete()
        return
    db.execute("UPDATE tasks SET status = 'ok' WHERE id = ?", (task_id,))
    db.commit()
    start_series(task_id)
    task = task_dict(task_id)
    await c.answer(t(lang, "saved"))
    await c.message.edit_text(
        "✅ " + task_text(lang, uid, task_id),
        parse_mode="HTML",
        reply_markup=keyboard(
            app_button(lang, "btn_edit", task=task_id, date=task["start"][:10]),
            [InlineKeyboardButton(text=t(lang, "btn_gcal"),
                                  url=gcal_link(task["title"], task["start"], task["location"], task["description"]))],
        ),
    )


@dp.callback_query(F.data.regexp(r"^ch:(ok|no):\d+$"))
async def change_action(c: CallbackQuery):
    _, action, change_id = c.data.split(":")
    uid = c.from_user.id
    lang = lang_of(uid)
    row = db.execute("SELECT task_id, fields FROM changes WHERE id = ? AND user_id = ?", (change_id, uid)).fetchone()
    db.execute("DELETE FROM changes WHERE id = ? AND user_id = ?", (change_id, uid))
    db.commit()
    task_id, data = row if row else (None, "{}")
    data = json.loads(data)
    if "plan" in data:  # план дня
        if action == "no":
            await c.answer(t(lang, "kept"))
            await c.message.delete()
            return
        apply_plan(uid, data["plan"])
        await c.answer(t(lang, "plan_applied"))
        await c.message.edit_text(c.message.html_text + "\n\n" + t(lang, "plan_applied"), parse_mode="HTML",
                                  reply_markup=keyboard(app_button(lang)))
        return
    exists = task_id and db.execute("SELECT title FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone()
    if not exists:
        await c.answer(t(lang, "not_actual"))
        await c.message.edit_reply_markup(reply_markup=None)
        return
    if action == "no":
        await c.answer(t(lang, "kept"))
        await c.message.delete()
        return
    if data.get("delete"):
        db.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid))
        db.commit()
        await c.answer(t(lang, "applied"))
        await c.message.edit_text(t(lang, "deleted", title=escape(exists[0])), parse_mode="HTML")
        return
    update_task(task_id, uid, data)
    await c.answer(t(lang, "applied"))
    await c.message.edit_text(t(lang, "updated") + "\n\n" + task_text(lang, uid, task_id), parse_mode="HTML")


@dp.callback_query(F.data.regexp(r"^(sz:(15|60)|dn):\d+$"))
async def reminder_action(c: CallbackQuery):
    *action, task_id = c.data.split(":")
    uid, task_id = c.from_user.id, int(task_id)
    lang = lang_of(uid)
    if not db.execute("SELECT 1 FROM tasks WHERE id = ? AND user_id = ?", (task_id, uid)).fetchone():
        await c.answer(t(lang, "task_gone"))
        return
    if action[0] == "dn":
        db.execute("UPDATE tasks SET done = 1 WHERE id = ?", (task_id,))
        db.commit()
        await c.answer(t(lang, "great"))
        await c.message.edit_text(t(lang, "done_header") + "\n\n" + card(lang, task_dict(task_id)), parse_mode="HTML")
        return
    at = datetime.now(TZ) + timedelta(minutes=int(action[1]))
    db.execute("UPDATE tasks SET snooze = ? WHERE id = ?", (f"{at:%Y-%m-%dT%H:%M}", task_id))
    db.commit()
    await c.answer(t(lang, "remind_at", time=f"{at:%H:%M}"))
    await c.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.regexp(r"^ev:(mv|dn):\d{4}-\d{2}-\d{2}$"))
async def evening_action(c: CallbackQuery):
    _, action, day = c.data.split(":")
    uid = c.from_user.id
    lang = lang_of(uid)
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
    await c.answer()
    await c.message.edit_text(t(lang, "moved" if action == "mv" else "marked", items=texts.word(lang, "item", len(rows))),
                              reply_markup=keyboard(app_button(lang)))


@dp.callback_query(F.data.regexp(r"^pl:\d{4}-\d{2}-\d{2}$"))
async def plan_action(c: CallbackQuery):
    await c.answer()
    await send_plan(c.bot, c.message.chat.id, c.from_user.id, c.data[3:])


# ── Планировщик ──────────────────────────────────────────────────

async def notify(bot: Bot, uid: int, task_id: int, header: str) -> None:
    lang = lang_of(uid)
    kb = keyboard([
        *[InlineKeyboardButton(text=texts.snooze_label(lang, m), callback_data=f"sz:{m}:{task_id}") for m in SNOOZE_MINUTES],
        InlineKeyboardButton(text=t(lang, "btn_done"), callback_data=f"dn:{task_id}"),
    ])
    try:
        await bot.send_message(uid, f"⏰ <b>{header}</b>\n\n" + card(lang, task_dict(task_id)), parse_mode="HTML", reply_markup=kb)
    except TelegramAPIError as e:
        logging.info("reminder %s: %s", uid, e)


async def send_reminders(bot: Bot, now: datetime) -> None:
    # ponytail: каждую минуту перебираем задачи на месяц вперёд в Python; при росте — таблица напоминаний с индексом по времени.
    rows = db.execute(
        "SELECT id, user_id, start, reminders, sent FROM tasks"
        " WHERE status = 'ok' AND done = 0 AND reminders != '' AND start BETWEEN ? AND ?",
        (f"{now - timedelta(days=1):%Y-%m-%d}", f"{now + timedelta(days=31):%Y-%m-%d}"),
    ).fetchall()
    for task_id, uid, start, reminders, sent in rows:
        sent_set = set(parse_offsets(sent))
        if not (due := due_offsets(start, parse_offsets(reminders), sent_set, now)):
            continue
        # Несколько просроченных сразу (бот был выключен) — одно сообщение, а не пачка.
        db.execute("UPDATE tasks SET sent = ? WHERE id = ?", (",".join(map(str, sent_set | set(due))), task_id))
        db.commit()
        lang = lang_of(uid)
        mins = round((event_time(start) - now).total_seconds() / 60)
        await notify(bot, uid, task_id, texts.time_left(lang, mins) if "T" in start else t(lang, "reminder"))


async def send_snoozed(bot: Bot, now: datetime) -> None:
    rows = db.execute(
        "SELECT id, user_id FROM tasks WHERE snooze <= ? AND done = 0", (f"{now:%Y-%m-%dT%H:%M}",)
    ).fetchall()
    for task_id, uid in rows:
        db.execute("UPDATE tasks SET snooze = NULL WHERE id = ?", (task_id,))
        db.commit()
        await notify(bot, uid, task_id, t(lang_of(uid), "remind_again"))


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
    day = f"{now:%Y-%m-%d}"
    for uid in due_users("digest_hour", "digest_sent", now):
        if not (rows := day_tasks(uid, day)):
            continue
        lang = lang_of(uid)
        lines = [f"• {start[11:16] or t(lang, 'all_day')} — {escape(title)}" for title, start in rows]
        untimed = any("T" not in start for _, start in rows)
        try:
            await bot.send_message(
                uid, t(lang, "digest") + "\n\n" + "\n".join(lines), parse_mode="HTML",
                reply_markup=keyboard(
                    [InlineKeyboardButton(text=t(lang, "btn_plan"), callback_data=f"pl:{day}")] if untimed else [],
                    app_button(lang),
                ),
            )
        except TelegramAPIError as e:
            logging.info("digest %s: %s", uid, e)


async def send_evenings(bot: Bot, now: datetime) -> None:
    day = f"{now:%Y-%m-%d}"
    for uid in due_users("evening_hour", "evening_sent", now):
        if not (rows := day_tasks(uid, day)):
            continue
        lang = lang_of(uid)
        lines = [f"• {start[11:16] or t(lang, 'all_day')} — {escape(title)}" for title, start in rows]
        try:
            await bot.send_message(
                uid,
                t(lang, "evening", items=texts.word(lang, "item", len(rows))) + "\n\n" + "\n".join(lines),
                parse_mode="HTML",
                reply_markup=keyboard(
                    [InlineKeyboardButton(text=t(lang, "btn_all_tomorrow"), callback_data=f"ev:mv:{day}"),
                     InlineKeyboardButton(text=t(lang, "btn_all_done"), callback_data=f"ev:dn:{day}")],
                    app_button(lang, "btn_review", date=day),
                ),
            )
        except TelegramAPIError as e:
            logging.info("evening %s: %s", uid, e)


async def send_weekly(bot: Bot, now: datetime) -> None:
    """Воскресенье, WEEKLY_HOUR: итоги недели — тем, у кого включены вечерние сводки."""
    if now.weekday() != 6 or now.hour != WEEKLY_HOUR:
        return
    day = f"{now:%Y-%m-%d}"
    uids = [uid for (uid,) in db.execute(
        "SELECT id FROM users WHERE evening_hour IS NOT NULL AND weekly_sent IS NOT ?", (day,)
    )]
    db.executemany("UPDATE users SET weekly_sent = ? WHERE id = ?", [(day, uid) for uid in uids])
    db.commit()
    for uid in uids:
        lang = lang_of(uid)
        try:
            await bot.send_message(uid, await week_text(uid, lang, now.date()), parse_mode="HTML",
                                   reply_markup=keyboard(app_button(lang)))
        except TelegramAPIError as e:
            logging.info("weekly %s: %s", uid, e)


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
            await send_weekly(bot, now)
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
    uid = feed_uid(request)
    rows = db.execute(
        "SELECT id, title, start, location, description FROM tasks WHERE user_id = ? AND status = 'ok'", (uid,)
    ).fetchall()
    return web.Response(text=ics(rows, t(lang_of(uid), "cal_name")), content_type="text/calendar", charset="utf-8")


async def subscribe(request: web.Request) -> web.Response:
    feed_uid(request)
    raise web.HTTPFound("webcal://" + f"{PUBLIC_URL}/cal/{request.match_info['name']}".split("://", 1)[1])


def webapp_user(request: web.Request) -> int:
    try:
        data = safe_parse_webapp_init_data(os.environ["BOT_TOKEN"], request.headers.get("Authorization", ""))
    except ValueError:
        data = None
    if not data or not data.user:
        raise web.HTTPUnauthorized()
    register(data.user.id, data.user.language_code)
    return data.user.id


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


API_COLUMNS = ("id", "title", "start", "location", "description", "source", "status", "done", "repeat", "series",
               "category", "priority", "origin", "subtasks", "reminders")


def api_rows(where: str, params: tuple) -> list[dict]:
    rows = db.execute(f"SELECT {', '.join(API_COLUMNS)} FROM tasks WHERE {where}", params).fetchall()
    return [dict(zip(API_COLUMNS, r)) | {"subtasks": json.loads(r[13] or "[]"), "reminders": parse_offsets(r[14])}
            for r in rows]


async def api_tasks(request: web.Request) -> web.Response:
    """Подтверждённые задачи за период [from, to), все неподтверждённые, настройки и статистика недели."""
    uid = webapp_user(request)
    today = datetime.now(TZ).date()
    start_from = normalize(request.query.get("from", "")) or f"{today}"
    start_to = normalize(request.query.get("to", "")) or "9999"
    reminders, smart, digest, evening, lang = db.execute(
        "SELECT reminders, smart_reminders, digest_hour, evening_hour, lang FROM users WHERE id = ?", (uid,)
    ).fetchone()
    return web.json_response({
        "tasks": api_rows("user_id = ? AND (status = 'pending' OR (start >= ? AND start < ?)) ORDER BY start",
                          (uid, start_from, start_to)),
        "settings": {"reminders": parse_offsets(reminders), "smart_reminders": bool(smart), "digest_hour": digest,
                     "evening_hour": evening, "lang": lang if lang in LANGS else "ru"},
        "week": week_stats(uid, today - timedelta(days=today.weekday())),
        "calendar": cal_links(uid),
    })


async def api_search(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    q = request.query.get("q", "").strip()[:100]
    if len(q) < 2:
        return web.json_response({"tasks": []})
    pattern = like(q)
    return web.json_response({"tasks": api_rows(
        "user_id = ? AND (py_lower(title) LIKE ? ESCAPE '\\' OR py_lower(description) LIKE ? ESCAPE '\\'"
        " OR py_lower(location) LIKE ? ESCAPE '\\' OR py_lower(subtasks) LIKE ? ESCAPE '\\')"
        " ORDER BY start >= ? DESC, start LIMIT 50",
        (uid, pattern, pattern, pattern, pattern, f"{datetime.now(TZ):%Y-%m-%d}"),
    )})


async def api_create(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    fields = fields_or_400(await json_body(request))
    if "title" not in fields or "start" not in fields:
        raise web.HTTPBadRequest()
    fields.setdefault("reminders", db.execute("SELECT reminders FROM users WHERE id = ?", (uid,)).fetchone()[0])
    fields |= {"status": "ok"}
    assert set(fields) <= TASK_COLUMNS
    task_id = db.execute(
        f"INSERT INTO tasks (user_id, origin, {', '.join(fields)}) VALUES (?, 'manual', {', '.join('?' * len(fields))})",
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
        return web.json_response({"undo": trash_tasks(uid, "series = ? AND start >= ?", (series, start))})
    if data.get("delete"):
        return web.json_response({"undo": trash_tasks(uid, "id = ?", (task_id,))})
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


async def api_undo(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    return web.json_response({"restored": restore_tasks(uid, str((await json_body(request)).get("batch", "")))})


async def api_plan(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    if not (day := normalize(str((await json_body(request)).get("date", "")))) or "T" in day:
        raise web.HTTPBadRequest()
    if (result := await make_plan(uid, day, lang_of(uid))) is None:
        raise web.HTTPServiceUnavailable()
    return web.json_response({"items": result[0], "comment": result[1]})


async def api_plan_apply(request: web.Request) -> web.Response:
    uid = webapp_user(request)
    items = (await json_body(request)).get("items")
    if not isinstance(items, list):
        raise web.HTTPBadRequest()
    plan = [[i.get("id"), i.get("start")] for i in items if isinstance(i, dict) and type(i.get("id")) is int]
    return web.json_response({"applied": apply_plan(uid, plan)})


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
    if "lang" in data:
        if data["lang"] not in LANGS:
            raise web.HTTPBadRequest()
        fields["lang"] = data["lang"]
    for key in ("digest_hour", "evening_hour"):
        if key in data:
            hour = data[key]
            if hour is not None and not (type(hour) is int and 0 <= hour <= 23):
                raise web.HTTPBadRequest()
            fields[key] = hour
    if fields:  # ключи только из списка выше
        db.execute(f"UPDATE users SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), uid))
        db.commit()
    if "lang" in fields:
        await set_menu(BOT, uid, fields["lang"])
    return web.json_response({"ok": True})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(Path(__file__).with_name("webapp.html"))


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/api/tasks", api_tasks),
        web.get("/api/search", api_search),
        web.post("/api/tasks", api_create),
        web.post(r"/api/tasks/{id:\d+}", api_update),
        web.post("/api/undo", api_undo),
        web.post("/api/plan", api_plan),
        web.post("/api/plan/apply", api_plan_apply),
        web.post("/api/settings", api_settings),
        web.get("/cal/{name}", feed),
        web.get("/subscribe/cal/{name}", subscribe),
    ])
    return app


async def setup_profile(bot: Bot) -> None:
    """Команды, описание и кнопка меню на всех языках (русский — по умолчанию)."""
    for lang in LANGS:
        code = None if lang == "ru" else lang
        await bot.set_my_commands([BotCommand(command=c, description=t(lang, f"cmd_{c}"))
                                   for c in ("today", "tasks", "plan", "week", "settings", "calendar", "help")],
                                  language_code=code)
        await bot.set_my_commands([BotCommand(command="task", description=t(lang, "cmd_task")),
                                   BotCommand(command="help", description=t(lang, "cmd_group_help"))],
                                  scope=BotCommandScopeAllGroupChats(), language_code=code)
        await bot.set_my_description(t(lang, "desc"), language_code=code)
        await bot.set_my_short_description(t(lang, "short_desc"), language_code=code)
    if PUBLIC_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Календарь", web_app=WebAppInfo(url=PUBLIC_URL)))


async def main():
    global BOT
    logging.basicConfig(level=logging.INFO)
    bot = BOT = Bot(os.environ["BOT_TOKEN"])
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await setup_profile(bot)
    reminders = asyncio.create_task(scheduler(bot))  # noqa: F841 — держим ссылку, чтобы задачу не собрал GC
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
