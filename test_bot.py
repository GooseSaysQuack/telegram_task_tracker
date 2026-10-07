import asyncio
import json
import os
from datetime import date, datetime
from types import SimpleNamespace as NS

os.environ["DB_PATH"] = ":memory:"
os.environ["TIMEZONE"] = "Europe/Moscow"
os.environ["BOT_TOKEN"] = "123:test"

import bot  # noqa: E402
import texts  # noqa: E402
from bot import TZ, db  # noqa: E402

# ── Даты, подписи, языки ──
assert "dates=20261009T190000%2F20261009T200000" in bot.gcal_link("Встреча", "2026-10-09T19:00", "")
assert "dates=20261231%2F20270101" in bot.gcal_link("Забрать заказ", "2026-12-31", "ПВЗ")
assert "details=%D0%92" in bot.gcal_link("x", "2030-01-01", "", "Взять")
assert bot.normalize("2026-10-09T19:00:00") == "2026-10-09T19:00" and bot.normalize("2026-10-09") == "2026-10-09"
assert bot.normalize("в пятницу") is None
assert bot.HINT.search("Созвон завтра в 15:00") and bot.HINT.search("call tomorrow") and bot.HINT.search("эртең жолугушуу")
assert not bot.HINT.search("ок, спасибо")
assert bot.human("ru", "2030-01-01T09:00") == "вт, 1 января, 09:00"
assert bot.human("en", "2030-01-01") == "Tue, Jan 1" and bot.human("ky", "2030-01-01") == "шй, 1-январь"
assert [texts.offset_label("ru", m) for m in (0, 15, 60, 90, 1440, 2880, 7200, 10080)] == [
    "в момент начала", "за 15 мин", "за 1 ч", "за 1 ч 30 мин", "за день", "за 2 дня", "за 5 дней", "за неделю"]
assert texts.offset_label("en", 2880) == "2 days before" and texts.offset_label("ky", 60) == "1 саат калганда"
assert [texts.time_left("ru", m) for m in (0, 25, 90, 1500)] == ["Начинается сейчас", "Через 25 мин", "Через 1 ч 30 мин", "Через 1 день 1 ч"]
assert texts.time_left("en", 1) == "Starting now" and texts.word("ru", "item", 5) == "5 дел" and texts.word("en", "item", 1) == "1 task"
assert texts.lang_from_code("ky") == "ky" and texts.lang_from_code("uk") == "ru" and texts.lang_from_code("de") == "en"
for lang in texts.LANGS:
    assert set(texts.T[lang]) == set(texts.T["ru"]), lang  # ни одного забытого перевода
    assert len(texts.t(lang, "short_desc")) <= 120 and len(texts.t(lang, "desc")) <= 512

# ── Проверка входных данных ──
assert bot.clean_offsets([60, 1440, 60, 0]) == "1440,60,0" and bot.clean_offsets([]) == ""
assert bot.clean_offsets([-5]) is None and bot.clean_offsets([60.5]) is None and bot.clean_offsets(list(range(11))) is None
assert json.loads(bot.clean_subtasks(["торт", {"text": " шары ", "done": True}])) == [
    {"text": "торт", "done": False}, {"text": "шары", "done": True}]
assert bot.clean_subtasks([""]) is None and bot.clean_subtasks(["x"] * 21) is None
assert bot.clean_fields({"category": "work", "priority": "high"}) == {"category": "work", "priority": "high"}
assert bot.clean_fields({"category": "hack"}) is None and bot.clean_fields({"priority": "urgent"}) is None
assert bot.like("100%_x") == "%100\\%\\_x%"

# ── Календарь .ics ──
cal = bot.ics([(1, "Созвон; план, итоги", "2026-10-09T19:00", "", "взять\nноутбук"), (2, "Забрать", "2026-10-10", "ПВЗ", "")])
assert "DTSTART:20261009T160000Z" in cal and "DTSTART;VALUE=DATE:20261010" in cal  # 19:00 МСК = 16:00 UTC
assert "SUMMARY:Созвон\\; план\\, итоги" in cal and "DESCRIPTION:взять\\nноутбук" in cal and cal.endswith("END:VCALENDAR\r\n")
assert bot.cal_path(42).startswith("cal/42-") and bot.cal_path(42) != bot.cal_path(43)

# ── Расписание напоминаний ──
at = lambda d, h, m=0: datetime(2030, 1, d, h, m, tzinfo=TZ)  # noqa: E731
t_ = "2030-01-02T10:00"
assert bot.due_offsets(t_, [1440, 60, 0], set(), at(1, 9, 59)) == []
assert bot.due_offsets(t_, [1440, 60, 0], set(), at(1, 10)) == [1440]
assert bot.due_offsets(t_, [1440, 60, 0], {1440, 60}, at(2, 10, 1)) == [0]
assert bot.due_offsets(t_, [1440, 60, 0], set(), at(2, 10, 5)) == []
assert bot.due_offsets("2030-01-02", [60], set(), at(2, 8)) == [60]  # весь день: отсчёт от 9:00
d = datetime.fromisoformat
assert bot.next_occurrence(d("2030-01-04T10:00"), "weekdays") == d("2030-01-07T10:00")
assert bot.next_occurrence(d("2030-01-31T10:00"), "monthly") == d("2030-02-28T10:00")


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text, kw.get("reply_markup")))

    async def send_chat_action(self, *a, **kw):
        pass

    async def me(self):
        return NS(username="tracker_bot")

    async def set_chat_menu_button(self, **kw):
        self.menu = kw


def run(fn, *args):
    b = FakeBot()
    asyncio.run(fn(b, *args))
    return b.sent


def first_lines(sent):
    return [(uid, text.split("\n")[0]) for uid, text, _ in sent]


def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def press(handler, data, uid, html=""):
    msg = NS(edited=None, html_text=html)
    async def edit_text(text, **kw): msg.edited = text
    async def edit_reply_markup(**kw): pass
    async def delete(): msg.edited = "deleted"
    answers = []
    async def answer(text=None, **kw): answers.append(text)
    msg.edit_text, msg.edit_reply_markup, msg.delete = edit_text, edit_reply_markup, delete
    await handler(NS(data=data, from_user=NS(id=uid, language_code="ru", first_name="Даня"), message=msg, answer=answer, bot=FakeBot()))
    return msg.edited, answers


db.execute("INSERT INTO users (id, lang, digest_hour, evening_hour, reminders) VALUES"
           " (1, 'ru', 9, 21, '60'), (2, 'en', NULL, NULL, '60'), (9, 'ru', 9, 21, '30'), (10, 'ru', 9, 21, '30')")
db.execute("UPDATE users SET smart_reminders = 0 WHERE id = 10")

# ── Напоминания: каждое один раз, просроченные пачкой — одним сообщением, на языке пользователя ──
db.executemany("INSERT INTO tasks (user_id, title, start, status, done, reminders) VALUES (?, ?, ?, ?, ?, ?)", [
    (1, "встреча", "2030-01-02T10:00", "ok", 0, "1440,60,15"),
    (1, "не подтверждена", "2030-01-02T10:00", "pending", 0, "60"),
    (1, "уже сделана", "2030-01-02T10:00", "ok", 1, "60"),
    (2, "all day", "2030-01-02", "ok", 0, "60"),
])
assert first_lines(run(bot.send_reminders, at(1, 10))) == [(1, "⏰ <b>Через 1 день</b>")]
assert run(bot.send_reminders, at(1, 10)) == []
assert first_lines(run(bot.send_reminders, at(2, 9, 50))) == [(1, "⏰ <b>Через 10 мин</b>")]
assert first_lines(run(bot.send_reminders, at(2, 8))) == [(2, "⏰ <b>Reminder</b>")]
db.execute("UPDATE tasks SET snooze = '2030-01-02T09:15' WHERE title = 'встреча'")
assert first_lines(run(bot.send_snoozed, at(2, 9, 15))) == [(1, "⏰ <b>Напоминаю ещё раз</b>")]

# ── Утренний план: кнопка «Спланировать», если есть дела без времени ──
db.execute("INSERT INTO tasks (user_id, title, start, status) VALUES (1, 'купить хлеб', '2030-01-02', 'ok')")
sent = run(bot.send_digests, at(2, 9))
assert first_lines(sent) == [(1, "☀️ <b>Доброе утро! План на сегодня:</b>")] and "pl:2030-01-02" in callbacks(sent[0][2])
assert run(bot.send_digests, at(2, 9, 30)) == []

# ── Повторы ──
first = f"{datetime.now(TZ):%Y-%m-%d}T08:00"
sid = db.execute("INSERT INTO tasks (user_id, title, start, status, repeat, subtasks) VALUES (9, 'Планёрка', ?, 'ok', 'weekly',"
                 " '[{\"text\": \"отчёт\", \"done\": true}]')", (first,)).lastrowid
bot.start_series(sid)
series = db.execute("SELECT start, subtasks FROM tasks WHERE series = ? ORDER BY start", (sid,)).fetchall()
assert series[0][0] == first and len(series) in (9, 10) and json.loads(series[1][1])[0]["done"] is False

# ── Пересечения ──
db.execute("INSERT INTO tasks (user_id, title, start, status) VALUES (9, 'Созвон', '2030-03-01T19:30', 'ok')")
assert bot.conflicts(9, "2030-03-01T19:00") == [("Созвон", "2030-03-01T19:30")] and bot.conflicts(9, "2030-03-01T20:30") == []


def new_task(**kw):
    return bot.NewTask(**{"title": "Рейс", "start": "2030-03-01T19:00", "location": "", "description": "", "category": "work",
                          "priority": "high", "subtasks": ["паспорт", " "], "reminders": [1440, 180], "repeat": ""} | kw)


# ── Новая задача от ИИ: категория, приоритет, подзадачи, умные напоминания (можно выключить) ──
b = FakeBot()
asyncio.run(bot.create_pending(b, 9, "ru", new_task(), "", None))
asyncio.run(bot.create_pending(b, 10, "ru", new_task(category="spam", priority="??"), "", None))
rows = db.execute("SELECT reminders, category, priority, subtasks FROM tasks WHERE title = 'Рейс' ORDER BY user_id").fetchall()
assert rows[0] == ("1440,180", "work", "high", '[{"text": "паспорт", "done": false}]')
assert rows[1][:3] == ("30", "other", "normal")  # умные напоминания выключены, мусор от ИИ заменён
assert "💼 <b>Рейс</b> 🔥" in b.sent[0][1] and "▫️ паспорт" in b.sent[0][1] and "⚠️ В это время уже: 19:30 Созвон" in b.sent[0][1]

# ── Правка от ИИ: только с подтверждением, только свои задачи ──
tid = db.execute("SELECT id FROM tasks WHERE user_id = 9 AND title = 'Созвон'").fetchone()[0]
move = bot.Change(id=tid, title="", start="2030-03-02T11:00", location="", description="", done=False, delete=False)
b = FakeBot()
assert asyncio.run(bot.propose_change(b, 9, "ru", tid, bot.change_data(move)))
assert not asyncio.run(bot.propose_change(b, 10, "ru", tid, bot.change_data(move)))
assert not asyncio.run(bot.propose_change(b, 9, "ru", tid, {"start": "когда-нибудь"}))
cb = callbacks(b.sent[0][2])[0]
assert asyncio.run(press(bot.change_action, cb, 10))[0] is None  # чужая кнопка
asyncio.run(press(bot.change_action, cb, 9))
assert db.execute("SELECT start FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "2030-03-02T11:00"

# ── План дня: ИИ расставляет только дела без времени этого пользователя ──
day = "2030-04-01"
db.executemany("INSERT INTO tasks (id, user_id, title, start, status, priority) VALUES (?, 9, ?, ?, 'ok', ?)", [
    (501, "Созвон с командой", f"{day}T10:00", "normal"), (502, "Позвонить в банк", day, "high"), (503, "Купить подарок", day, "low"),
])


async def fake_plan(contents, schema=None):
    assert "Позвонить в банк" in contents[0] and "10:00 Созвон с командой" in contents[0]
    return bot.Plan(comment="Важное — с утра.", items=[
        bot.PlanItem(id=502, time="09:00", minutes=15), bot.PlanItem(id=503, time="25:00", minutes=30),  # кривое время
        bot.PlanItem(id=501, time="12:00", minutes=10), bot.PlanItem(id=999, time="13:00", minutes=10),  # встреча / чужое
    ])
real_ask, bot.ask_model = bot.ask_model, fake_plan
items, comment = asyncio.run(bot.make_plan(9, day, "ru"))
assert [(i["id"], i["start"]) for i in items] == [(502, f"{day}T09:00")] and comment == "Важное — с утра."
sent = run(bot.send_plan, 9, 9, day)
assert "09:00–09:15  Позвонить в банк" in sent[0][1]
asyncio.run(press(bot.change_action, callbacks(sent[0][2])[0], 9, html=sent[0][1]))
assert db.execute("SELECT start FROM tasks WHERE id = 502").fetchone()[0] == f"{day}T09:00"
assert bot.apply_plan(10, [[503, f"{day}T15:00"]]) == 0  # чужую задачу планом не двинуть
bot.ask_model = real_ask

# ── Продуктивность недели ──
db.executemany("INSERT INTO tasks (user_id, title, start, status, done, category, origin) VALUES (2, ?, ?, 'ok', ?, ?, ?)", [
    ("a", "2030-05-06T10:00", 1, "work", "ai"), ("b", "2030-05-07", 0, "work", "manual"), ("c", "2030-05-12", 1, "health", "ai"),
    ("next week", "2030-05-13", 0, "work", "ai"),
])
assert bot.week_stats(2, date(2030, 5, 6)) == {"total": 3, "done": 2, "ai": 2, "by_category": {"work": [1, 2], "health": [1, 1]}}

# ── Удаление с «Отменить» ──
batch = bot.trash_tasks(2, "title = ?", ("c",))
assert not db.execute("SELECT 1 FROM tasks WHERE title = 'c'").fetchone()
assert bot.restore_tasks(1, batch) == 0  # чужую корзину не восстановить
assert bot.restore_tasks(2, batch) == 1 and db.execute("SELECT category FROM tasks WHERE title = 'c'").fetchone() == ("health",)

# ── Пачка пересылок: один запрос к ИИ со всей перепиской ──
calls = []


async def fake_respond(b, chat_id, uid, text, ref, parts, mode):
    calls.append((text, mode))
real_respond, real_delay = bot.respond, bot.BATCH_DELAY
bot.respond, bot.BATCH_DELAY = fake_respond, 0.05


def fwd(text, name, minute):
    return NS(text=text, caption=None, forward_origin=NS(date=datetime(2030, 1, 1, 12, minute, tzinfo=TZ), sender_user=NS(first_name=name)))


async def batch_flow():
    for m in (fwd("в пятницу в 7?", "Аня", 1), fwd("давай в 8, в 7 не успею", "Миша", 2), fwd("ок, в 8 у ЦУМа", "Аня", 3)):
        bot.batches.setdefault(5, []).append(m)
        if timer := bot.batch_timers.get(5):
            timer.cancel()
        bot.batch_timers[5] = asyncio.create_task(bot.flush_batch(FakeBot(), 5, 5))
    await asyncio.sleep(0.2)
asyncio.run(batch_flow())
assert len(calls) == 1 and calls[0][1] == bot.BATCH_MODE
assert calls[0][0].splitlines() == ["[2030-01-01 12:01] Аня: в пятницу в 7?", "[2030-01-01 12:02] Миша: давай в 8, в 7 не успею",
                                    "[2030-01-01 12:03] Аня: ок, в 8 у ЦУМа"]
bot.respond, bot.BATCH_DELAY = real_respond, real_delay

# ── Встроенный режим: «Добавить себе» — только с верной подписью и только запустившим бота ──
share_id = db.execute("SELECT id FROM tasks WHERE title = 'Рейс' AND user_id = 9").fetchone()[0]
good = f"sh:{share_id}:{bot.sign(f'share:{share_id}', 12)}"
_, answers = asyncio.run(press(bot.share_add, f"sh:{share_id}:000000000000", 1))
assert answers == [None] and not db.execute("SELECT 1 FROM tasks WHERE user_id = 1 AND title = 'Рейс'").fetchone()
_, answers = asyncio.run(press(bot.share_add, good, 777))
assert answers == [texts.t("ru", "inline_start")]
_, answers = asyncio.run(press(bot.share_add, good, 1))
assert answers == [texts.t("ru", "inline_added")]
assert db.execute("SELECT subtasks, origin, status FROM tasks WHERE user_id = 1 AND title = 'Рейс'").fetchone() == (
    '[{"text": "паспорт", "done": false}]', "shared", "ok")
_, answers = asyncio.run(press(bot.share_add, good, 1))
assert answers == [texts.t("ru", "inline_already")]


# ── /task в группе ──
class GroupMsg:
    def __init__(self, uid, reply_to=None):
        self.from_user, self.reply_to_message = NS(id=uid, language_code="ru"), reply_to
        self.chat = NS(id=-500, title="Работа")
        self.replies, self.reactions = [], []
        self.bot = FakeBot()

    async def reply(self, text, **kw): self.replies.append(text)
    async def react(self, r): self.reactions.append(r)


async def fake_analyze(*a):
    return bot.Result(reply="", update=[], create=[new_task(title="Созвон", start="2030-06-01T15:00")])


async def fake_input(m):
    return "голосовое", datetime.now(TZ), []
real_analyze, real_input = bot.analyze, bot.message_input
bot.analyze, bot.message_input = fake_analyze, fake_input
voice = NS(as_=lambda b: voice)
m = GroupMsg(9)
asyncio.run(bot.group_task(m))
assert m.replies == [texts.t("ru", "task_need_reply")]
m = GroupMsg(12345, voice)
asyncio.run(bot.group_task(m))
assert m.replies == [texts.t("ru", "start_first")]
m = GroupMsg(9, voice)
asyncio.run(bot.group_task(m))
assert m.reactions and not m.replies and m.bot.sent[0][0] == 9
bot.analyze, bot.message_input = real_analyze, real_input

# ── Вечерний итог: «Всё на завтра» ──
db.execute("INSERT INTO tasks (user_id, title, start, status) VALUES (10, 'Купить хлеб', '2030-03-05', 'ok'),"
           " (10, 'Позвонить маме', '2030-03-05T18:00', 'ok')")
sent = run(bot.send_evenings, datetime(2030, 3, 5, 21, 0, tzinfo=TZ))
assert any(uid == 10 and "не отмечено 2 дела" in text for uid, text, _ in sent)
asyncio.run(press(bot.evening_action, "ev:mv:2030-03-05", 10))
assert sorted(s for (s,) in db.execute("SELECT start FROM tasks WHERE user_id = 10 AND title IN ('Купить хлеб', 'Позвонить маме')")) == [
    "2030-03-06", "2030-03-06T18:00"]

# ── @бот в группе: подсказка, разбор ответа (голосовое) или текста после упоминания ──
bot.analyze, bot.message_input = fake_analyze, fake_input
seen = []


async def spy_input(m):
    seen.append(m)
    return "x", datetime.now(TZ), []
bot.message_input = spy_input
m = GroupMsg(9)
m.text, m.caption = "@Tracker_Bot", None
asyncio.run(bot.group(m))
assert "@tracker_bot" in m.replies[0] and not seen  # просто упоминание — подсказка, без запроса к ИИ
m = GroupMsg(9, voice)
m.text, m.caption = "@tracker_bot", None
asyncio.run(bot.group(m))
assert seen == [voice] and m.reactions  # ответ на голосовое — разбираем его
m = GroupMsg(9)
m.text, m.caption = "@tracker_bot завтра в 10 созвон", None
asyncio.run(bot.group(m))
assert seen[-1] is m and m.reactions  # текст после упоминания
bot.analyze, bot.message_input = real_analyze, real_input

# ── Выбор языка кнопкой: сохраняется, подсказка сразу на новом языке ──
edited, answers = asyncio.run(press(bot.choose_language, "lang:en", 9))
assert bot.lang_of(9) == "en" and answers == ["✅ Language changed"] and edited.startswith("<b>Hi")
asyncio.run(press(bot.choose_language, "lang:ky", 9))
assert bot.lang_of(9) == "ky"
asyncio.run(press(bot.choose_language, "lang:ru", 9))

# ── Память диалога: последние реплики, только свежие ──
for i in range(8):
    bot.remember(42, "Пользователь", f"реплика {i}")
assert [x[2] for x in bot.dialogs[42]] == [f"реплика {i}" for i in range(2, 8)]
assert "Пользователь: реплика 7" in bot.dialog_block(42) and bot.dialog_block(43) == ""
bot.dialogs[42] = [(datetime.now(TZ) - bot.DIALOG_TTL, "Пользователь", "давно")]
assert bot.dialog_block(42) == ""
assert "v=" + bot.APP_VERSION in bot.app_url() and "task=5" in bot.app_url(task=5)
print("ok")
