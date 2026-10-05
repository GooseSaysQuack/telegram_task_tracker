import asyncio
import os
from datetime import datetime, timedelta

os.environ["DB_PATH"] = ":memory:"
os.environ["TIMEZONE"] = "Europe/Moscow"
os.environ["BOT_TOKEN"] = "123:test"

import json  # noqa: E402
from types import SimpleNamespace as NS  # noqa: E402

from bot import (  # noqa: E402
    HINT, TZ, Change, NewTask, cal_path, card, change_action, change_data, clean_offsets, conflicts, create_pending, db,
    due_offsets, evening_action, gcal_link, human, ics, next_occurrence, normalize, offset_label, propose_change,
    send_digests, send_evenings, send_reminders, send_snoozed, start_series, time_left,
)

assert "dates=20261009T190000%2F20261009T200000" in gcal_link("Встреча", "2026-10-09T19:00", "")
assert "dates=20261231%2F20270101" in gcal_link("Забрать заказ", "2026-12-31", "ПВЗ")
assert "ctz=Europe%2FMoscow" in gcal_link("x", "2026-10-09", "")
assert normalize("2026-10-09T19:00:00") == "2026-10-09T19:00" and normalize("2026-10-09") == "2026-10-09"
assert normalize("в пятницу") is None
assert HINT.search("Созвон завтра в 15:00") and HINT.search("митап 12 окт") and not HINT.search("ок, спасибо")
assert human("2030-01-01T09:00") == "вт, 1 января, 09:00" and human("2030-01-01") == "вт, 1 января"
assert card("<Митап>", "2030-01-01T09:00", "", "Работа", "Взять <ноутбук>") == (
    "<b>&lt;Митап&gt;</b>\n🗓 вт, 1 января, 09:00\n📝 Взять &lt;ноутбук&gt;\n💬 Работа")
assert "details=%D0%92" in gcal_link("x", "2030-01-01", "", "Взять")

cal = ics([(1, "Созвон; план, итоги", "2026-10-09T19:00", "", "взять\nноутбук"), (2, "Забрать заказ", "2026-10-10", "ПВЗ", "")])
assert "DTSTART:20261009T160000Z" in cal and "DTEND:20261009T170000Z" in cal  # 19:00 МСК = 16:00 UTC
assert "DTSTART;VALUE=DATE:20261010" in cal and "DTEND;VALUE=DATE:20261011" in cal
assert "SUMMARY:Созвон\\; план\\, итоги" in cal and "DESCRIPTION:взять\\nноутбук" in cal and cal.endswith("END:VCALENDAR\r\n")
assert cal_path(42).startswith("cal/42-") and cal_path(42) != cal_path(43)

# Подписи и проверка набора напоминаний
assert [offset_label(m) for m in (0, 15, 60, 90, 1440, 2880, 7200, 10080)] == [
    "в момент начала", "за 15 мин", "за 1 ч", "за 1 ч 30 мин", "за день", "за 2 дня", "за 5 дней", "за неделю"]
assert [time_left(timedelta(minutes=m)) for m in (0, 25, 90, 1500)] == [
    "Начинается сейчас", "Через 25 мин", "Через 1 ч 30 мин", "Через 1 день 1 ч"]
assert clean_offsets([60, 1440, 60, 0]) == "1440,60,0" and clean_offsets([]) == ""
assert clean_offsets([-5]) is None and clean_offsets([60.5]) is None and clean_offsets("60") is None
assert clean_offsets(list(range(11))) is None and clean_offsets([31 * 1440]) is None

# Когда срабатывают: начало в 10:00, напоминания за день, за час и в момент начала
t = "2030-01-02T10:00"
at = lambda d, h, m=0: datetime(2030, 1, d, h, m, tzinfo=TZ)  # noqa: E731
assert due_offsets(t, [1440, 60, 0], set(), at(1, 9, 59)) == []
assert due_offsets(t, [1440, 60, 0], set(), at(1, 10)) == [1440]
assert due_offsets(t, [1440, 60, 0], {1440}, at(2, 9, 30)) == [60]
assert due_offsets(t, [1440, 60, 0], {1440, 60}, at(2, 10, 1)) == [0]
assert due_offsets(t, [1440, 60, 0], set(), at(2, 10, 5)) == []  # событие прошло — молчим
assert due_offsets("2030-01-02", [60], set(), at(2, 8)) == [60]  # весь день: отсчёт от 9:00


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text.split("\n")[0]))


def run(fn, now):
    bot = FakeBot()
    asyncio.run(fn(bot, now))
    return bot.sent


# Несколько напоминаний: каждое один раз; просроченные пачкой — одним сообщением.
db.execute("INSERT INTO users (id, digest_hour) VALUES (1, 9), (2, NULL)")
db.executemany(
    "INSERT INTO tasks (user_id, title, start, status, done, reminders) VALUES (?, ?, ?, ?, ?, ?)",
    [
        (1, "встреча", "2030-01-02T10:00", "ok", 0, "1440,60,15"),
        (1, "не подтверждена", "2030-01-02T10:00", "pending", 0, "60"),
        (1, "уже сделана", "2030-01-02T10:00", "ok", 1, "60"),
        (1, "без напоминаний", "2030-01-02T10:00", "ok", 0, ""),
        (2, "весь день", "2030-01-02", "ok", 0, "60"),
    ],
)
assert run(send_reminders, at(1, 10)) == [(1, "⏰ <b>Через 1 день</b>")]
assert run(send_reminders, at(1, 10)) == []
assert run(send_reminders, at(2, 9, 50)) == [(1, "⏰ <b>Через 10 мин</b>")]  # за час и за 15 мин — одно сообщение
assert run(send_reminders, at(2, 9, 55)) == []
assert run(send_reminders, at(2, 8)) == [(2, "⏰ <b>Напоминание</b>")]

# Отложенное напоминание
db.execute("UPDATE tasks SET snooze = '2030-01-02T09:15' WHERE title = 'встреча'")
assert run(send_snoozed, at(2, 9, 14)) == []
assert run(send_snoozed, at(2, 9, 15)) == [(1, "⏰ <b>Напоминаю ещё раз</b>")]
assert run(send_snoozed, at(2, 9, 16)) == []

# Утренний план: в свой час, один раз в день; выключенный — не приходит.
assert run(send_digests, at(2, 8)) == []
assert run(send_digests, at(2, 9)) == [(1, "☀️ <b>Доброе утро! План на сегодня:</b>")]
assert run(send_digests, at(2, 9, 30)) == []

# ── Повторы ──
d = lambda s: datetime.fromisoformat(s)  # noqa: E731
assert next_occurrence(d("2030-01-04T10:00"), "weekdays") == d("2030-01-07T10:00")  # пт → пн
assert next_occurrence(d("2030-01-31T10:00"), "monthly") == d("2030-02-28T10:00")
assert next_occurrence(d("2030-12-15"), "monthly") == d("2031-01-15")
assert next_occurrence(d("2030-01-01"), "weekly") == d("2030-01-08")
now_real = datetime.now(TZ)
first = f"{now_real:%Y-%m-%d}T08:00"
sid = db.execute("INSERT INTO tasks (user_id, title, start, status, repeat) VALUES (9, 'Планёрка', ?, 'ok', 'weekly')",
                 (first,)).lastrowid
start_series(sid)
series = [s for (s,) in db.execute("SELECT start FROM tasks WHERE series = ? ORDER BY start", (sid,))]
assert series[0] == first and len(series) in (9, 10) and series[1][11:] == "08:00", series  # ~60 дней вперёд
start_series(sid)  # повторный вызов не плодит копии
assert db.execute("SELECT COUNT(*) FROM tasks WHERE series = ?", (sid,)).fetchone()[0] == len(series)

# ── Пересечения ──
db.execute("INSERT INTO tasks (user_id, title, start, status) VALUES (9, 'Созвон', '2030-03-01T19:30', 'ok')")
assert conflicts(9, "2030-03-01T19:00") == [("Созвон", "2030-03-01T19:30")]
assert conflicts(9, "2030-03-01T20:30") == [] and conflicts(9, "2030-03-01") == []


class Bot2(FakeBot):
    async def send_message(self, uid, text, **kw):
        self.sent.append((uid, text, kw.get("reply_markup")))


def button_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


async def press(handler, data, uid):
    msg = NS(edited=None)
    async def edit_text(text, **kw): msg.edited = text
    async def edit_reply_markup(**kw): pass
    async def delete(): msg.edited = "deleted"
    async def answer(*a, **kw): pass
    msg.edit_text, msg.edit_reply_markup, msg.delete = edit_text, edit_reply_markup, delete
    await handler(NS(data=data, from_user=NS(id=uid), message=msg, answer=answer))
    return msg.edited


# ── Умные напоминания: ИИ выбирает, но можно выключить в настройках ──
db.execute("INSERT INTO users (id, reminders, smart_reminders) VALUES (9, '30', 1), (10, '30', 0)")
b = Bot2()
asyncio.run(create_pending(b, 9, NewTask(title="Рейс", start="2030-03-01T19:00", location="", description="",
                                         reminders=[1440, 180], repeat=""), "", None))
asyncio.run(create_pending(b, 10, NewTask(title="Рейс", start="2030-03-01T19:00", location="", description="",
                                          reminders=[1440, 180], repeat=""), "", None))
assert [r for (r,) in db.execute("SELECT reminders FROM tasks WHERE title = 'Рейс' ORDER BY user_id")] == ["1440,180", "30"]
assert "⚠️ В это время уже: 19:30 Созвон" in b.sent[0][1] and "🔔 Напомню за день, за 3 ч" in b.sent[0][1]

# ── Правка от ИИ: не применяется без кнопки; чужие задачи не трогает ──
tid = db.execute("SELECT id FROM tasks WHERE user_id = 9 AND title = 'Созвон'").fetchone()[0]
b = Bot2()
move = Change(id=tid, title="", start="2030-03-02T11:00", location="", description="", done=False, delete=False)
assert asyncio.run(propose_change(b, 9, tid, change_data(move)))
assert not asyncio.run(propose_change(b, 10, tid, change_data(move)))  # чужая задача
assert not asyncio.run(propose_change(b, 9, tid, {"start": "когда-нибудь"}))  # мусор от ИИ
assert db.execute("SELECT start FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "2030-03-01T19:30"  # ещё не применено
cb = button_data(b.sent[0][2])[0]
assert asyncio.run(press(change_action, cb, 10)) is None  # чужая кнопка не срабатывает
asyncio.run(press(change_action, cb, 9))
assert db.execute("SELECT start FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "2030-03-02T11:00"
assert asyncio.run(press(change_action, cb, 9)) is None  # повторное нажатие ничего не делает
b = Bot2()
cancel = Change(id=tid, title="", start="", location="", description="", done=False, delete=True)
asyncio.run(propose_change(b, 9, tid, change_data(cancel)))
asyncio.run(press(change_action, button_data(b.sent[0][2])[0], 9))
assert not db.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone()

# ── Вечерний итог: в свой час, с кнопкой «всё на завтра» ──
db.execute("UPDATE users SET evening_hour = 21 WHERE id = 9")
db.execute("INSERT INTO tasks (user_id, title, start, status) VALUES (9, 'Купить хлеб', '2030-03-05', 'ok'),"
           " (9, 'Позвонить маме', '2030-03-05T18:00', 'ok')")
b = Bot2()
asyncio.run(send_evenings(b, datetime(2030, 3, 5, 20, 0, tzinfo=TZ)))
assert b.sent == []
asyncio.run(send_evenings(b, datetime(2030, 3, 5, 21, 0, tzinfo=TZ)))
assert len(b.sent) == 1 and "не отмечено 2 дела" in b.sent[0][1]
asyncio.run(press(evening_action, "ev:mv:2030-03-05", 9))
assert sorted(s for (s,) in db.execute("SELECT start FROM tasks WHERE title IN ('Купить хлеб', 'Позвонить маме')")) == [
    "2030-03-06", "2030-03-06T18:00"]

# ── /task в группе: ответом на сообщение; задача только автору команды ──
import bot as bot_module  # noqa: E402


class GroupMsg:
    def __init__(self, uid, reply_to=None):
        self.from_user, self.reply_to_message = NS(id=uid), reply_to
        self.chat = NS(id=-500, title="Работа")
        self.replies, self.reactions = [], []
        self.bot = Bot2()
        async def get_me(): return NS(username="tracker_bot")
        self.bot.get_me = get_me

    async def reply(self, text, **kw): self.replies.append(text)
    async def react(self, r): self.reactions.append(r)


async def fake_analyze(m, mode, ctx):
    return bot_module.Result(reply="", update=[], create=[NewTask(
        title="Созвон", start="2030-04-01T15:00", location="", description="", reminders=[], repeat="")])
real_analyze, bot_module.analyze = bot_module.analyze, fake_analyze
voice = NS(as_=lambda b: voice)
m = GroupMsg(9)
asyncio.run(bot_module.group_task(m))
assert "Ответь командой /task" in m.replies[0]  # без ответа на сообщение
m = GroupMsg(12345, voice)
asyncio.run(bot_module.group_task(m))
assert "Старт" in m.replies[0]  # не запускал бота — в личку не написать
m = GroupMsg(9, voice)
asyncio.run(bot_module.group_task(m))
assert m.reactions and not m.replies and m.bot.sent[0][0] == 9  # карточка в личку автору, в чате только 👍
bot_module.analyze = real_analyze
print("ok")
