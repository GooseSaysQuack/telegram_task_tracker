"""Тексты бота на трёх языках. Кыргызский стоит вычитать носителю языка."""

from datetime import date, timedelta

LANGS = ("ru", "en", "ky")
LANG_NAMES = {"ru": "русский", "en": "английский", "ky": "кыргызский"}  # для подсказки ИИ, на каком языке отвечать


def lang_from_code(code: str | None) -> str:
    """Язык интерфейса по language_code из Telegram."""
    code = (code or "").split("-")[0].lower()
    if code == "ky":
        return "ky"
    if not code or code in ("ru", "uk", "be", "kk", "uz", "tg"):
        return "ru"
    return "en"


def plural(lang: str, n: int, forms: tuple[str, ...]) -> str:
    """forms: ru — (одна, две, пять); en — (one, many); ky — (одна форма)."""
    if lang == "ru":
        if n % 10 == 1 and n % 100 != 11:
            return forms[0]
        return forms[1] if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else forms[2]
    if lang == "en":
        return forms[0] if n == 1 else forms[1]
    return forms[0]


WORDS = {
    "ru": {"day": ("день", "дня", "дней"), "item": ("дело", "дела", "дел"),
           "reminder": ("напоминание", "напоминания", "напоминаний")},
    "en": {"day": ("day", "days"), "item": ("task", "tasks"), "reminder": ("reminder", "reminders")},
    "ky": {"day": ("күн",), "item": ("иш",), "reminder": ("эскертме",)},
}


def word(lang: str, key: str, n: int) -> str:
    return f"{n} {plural(lang, n, WORDS[lang][key])}"


WD_SHORT = {
    "ru": ["пн", "вт", "ср", "чт", "пт", "сб", "вс"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "ky": ["дш", "шй", "шр", "бш", "жм", "иш", "жк"],
}
MONTHS = {
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "ky": ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"],
}


def day_name(lang: str, d: date, today: date) -> str:
    if d == today:
        return t(lang, "today")
    if d == today + timedelta(days=1):
        return t(lang, "tomorrow")
    wd, month = WD_SHORT[lang][d.weekday()], MONTHS[lang][d.month - 1]
    return {"ru": f"{wd}, {d.day} {month}", "en": f"{wd}, {month} {d.day}", "ky": f"{wd}, {d.day}-{month}"}[lang]


def offset_label(lang: str, m: int) -> str:
    """Подпись напоминания: «за 1 ч», «1 hour before», «1 саат калганда»."""
    if m == 0:
        return {"ru": "в момент начала", "en": "at start", "ky": "башталганда"}[lang]
    if m == 7 * 1440:
        return {"ru": "за неделю", "en": "1 week before", "ky": "1 жума калганда"}[lang]
    if m % 1440 == 0:
        d = m // 1440
        span = word(lang, "day", d) if lang != "ru" or d != 1 else "день"
    else:
        h, mm = divmod(m, 60)
        units = {"ru": ("ч", "мин"), "en": ("h", "min"), "ky": ("саат", "мүнөт")}[lang]
        span = " ".join(filter(None, [h and f"{h} {units[0]}", mm and f"{mm} {units[1]}"]))
    return {"ru": f"за {span}", "en": f"{span} before", "ky": f"{span} калганда"}[lang]


def time_left(lang: str, mins: int) -> str:
    if mins <= 1:
        return t(lang, "starts_now")
    d, rest = divmod(mins, 1440)
    h, m = divmod(rest, 60)
    units = {"ru": ("ч", "мин"), "en": ("h", "min"), "ky": ("саат", "мүнөт")}[lang]
    span = word(lang, "day", d) + (f" {h} {units[0]}" if h else "") if d else \
        " ".join(filter(None, [h and f"{h} {units[0]}", m and f"{m} {units[1]}"]))
    return {"ru": f"Через {span}", "en": f"In {span}", "ky": f"{span} калды"}[lang]


def snooze_label(lang: str, m: int) -> str:
    """Кнопка «напомнить позже»: ⏰ +15 мин / +1 ч."""
    h, mm = divmod(m, 60)
    units = {"ru": ("ч", "мин"), "en": ("h", "min"), "ky": ("саат", "мүнөт")}[lang]
    return "⏰ +" + (f"{h} {units[0]}" if h else f"{mm} {units[1]}")


CATEGORIES = ("work", "personal", "shopping", "health", "study", "other")
CATEGORY_ICONS = {"work": "💼", "personal": "🏠", "shopping": "🛒", "health": "🩺", "study": "📚", "other": "📌"}
PRIORITIES = ("high", "normal", "low")

T = {
    "ru": {
        "menu": "Календарь", "cal_name": "Задачи из Telegram",
        "today": "Сегодня", "tomorrow": "Завтра", "all_day": "весь день", "starts_now": "Начинается сейчас",
        "cat_work": "Работа", "cat_personal": "Личное", "cat_shopping": "Покупки", "cat_health": "Здоровье",
        "cat_study": "Учёба", "cat_other": "Другое", "prio_high": "🔥 Важно", "prio_low": "Не срочно",
        "rep_daily": "каждый день", "rep_weekdays": "по будням", "rep_weekly": "каждую неделю", "rep_monthly": "каждый месяц",
        "help": (
            "<b>Привет{name}! Я собираю твои дела из чатов.</b>\n\n"
            "📨 Пересылай мне сообщения — можно сразу пачкой, я пойму переписку целиком\n"
            "🎙 Надиктуй голосовое: «завтра в 10 стоматолог»\n"
            "📸 Пришли скриншот переписки, афишу или билет\n"
            "👥 Добавь меня в рабочий чат — замечу новые дела и переносы\n"
            "↗️ В любом чате набери @{bot} — и поделись встречей с собеседником\n\n"
            "<b>Можно просто написать</b>\n"
            "• «что у меня завтра?», «когда я свободен в четверг?»\n"
            "• «перенеси встречу с Мишей на субботу», «отмени стоматолога»\n"
            "• «каждый вторник в 10 планёрка», «купить на ДР торт, шарики, свечи»\n\n"
            "⏰ Напомню, когда нужно, ☀️ утром пришлю план, 🌙 вечером — что не успел, 📊 по воскресеньям — итоги недели\n\n"
            "<b>Команды</b>\n"
            "/today — задачи на сегодня\n/tasks — ближайшие задачи\n/plan — ИИ спланирует день\n"
            "/week — продуктивность и обзор недели\n/settings — напоминания, язык, утро и вечер\n"
            "/calendar — показывать задачи в календаре телефона\n/help — эта подсказка"
        ),
        "settings_text": "Напоминания, язык, утренний план и вечерний итог — всё в настройках:",
        "btn_settings": "⚙️ Открыть настройки",
        "no_public_url": "Недоступно: не задан PUBLIC_URL.",
        "calendar_text": "Подключи календарь один раз — подтверждённые задачи будут появляться в нём сами.\n\nДругой календарь — добавь его по ссылке:\n{url}",
        "today_empty": "На сегодня задач нет 🎉", "tasks_empty": "Задач нет.",
        "unknown_cmd": "Не знаю такой команды. Список — /help",
        "doc_unsupported": "Пришли текст, голосовое, фото или скриншот — файлы других типов я не читаю.",
        "ai_failed": "Не получилось разобрать, попробуй позже.",
        "nothing_found": "Не нашёл тут дел. Можно спросить: «что у меня завтра?» или «перенеси встречу на субботу».",
        "group_help": "Я замечаю в этом чате встречи, дедлайны и переносы и присылаю их в личку тем, кто запустил меня. В чат я ничего не пишу.\n\n🎙 Голосовые и фото сам не разбираю. Ответь на такое сообщение командой /task — и я пришлю задачу тебе в личку.",
        "private_only": "Эта команда работает в личке — там твои задачи не видны другим.",
        "task_need_reply": "Ответь командой /task на сообщение, голосовое или фото — и я сделаю из него задачу.",
        "start_first": "Сначала нажми «Старт» у меня в личке — туда придёт задача.",
        "nothing_here": "Не нашёл тут дела.",
        "group_welcome": "👋 Привет! Я замечаю в этом чате встречи, дедлайны и переносы и присылаю их в личку — тем, кто запустил меня. Здесь я молчу.\n\nЧтобы получать задачи из этого чата — нажмите кнопку ниже и «Старт».",
        "group_privacy": "⚠️ Сейчас я не вижу сообщений чата. Сделайте меня администратором или отключите режим приватности в @BotFather (/setprivacy → Disable) и добавьте меня заново.",
        "group_voice": "🎙 Голосовое или фото — ответьте на него командой /task.",
        "btn_get_tasks": "🚀 Получать задачи в личку",
        "handled": "Уже обработано", "saved": "Сохранено",
        "btn_add": "✅ Добавить", "btn_apply": "✅ Применить", "btn_edit": "✏️ Изменить и настроить напоминания",
        "btn_gcal": "📅 В Google Календарь", "btn_open": "🗓 Открыть календарь", "btn_done": "✅ Сделано",
        "btn_all_tomorrow": "📅 Всё на завтра", "btn_all_done": "✅ Всё сделано", "btn_review": "🗓 Разобрать в календаре",
        "btn_plan": "✨ Спланировать день", "btn_iphone": "🍎 iPhone / Mac", "btn_google": "📅 Google Календарь",
        "remind": "🔔 Напомню {list}", "no_remind": "🔕 Без напоминаний", "repeat": "🔁 Повторять {label}",
        "conflict": "⚠️ В это время уже: {list}",
        "ch_delete": "🗑 <b>Удалить задачу?</b>", "ch_edit": "✏️ <b>Изменить «{title}»?</b>", "ch_done": "✅ Отметить выполненной",
        "not_actual": "Уже неактуально", "kept": "Оставил как было", "deleted": "🗑 Удалено: <b>{title}</b>",
        "updated": "✅ <b>Обновлено</b>", "applied": "Готово",
        "task_gone": "Задача удалена", "great": "Отлично!", "done_header": "✅ <b>Сделано</b>", "remind_at": "Напомню в {time}",
        "reminder": "Напоминание", "remind_again": "Напоминаю ещё раз",
        "moved": "📅 Перенёс на завтра: {items}", "marked": "✅ Отметил выполненными: {items}",
        "evening": "🌙 <b>Сегодня не отмечено {items}:</b>", "digest": "☀️ <b>Доброе утро! План на сегодня:</b>",
        "plan_header": "✨ <b>План дня · {day}</b>", "plan_nothing": "Планировать нечего: на этот день нет дел без времени.",
        "plan_failed": "Не получилось составить план, попробуй позже.", "plan_applied": "✅ План применён",
        "week_header": "📊 <b>Итоги недели {range}</b>", "week_done": "✅ Выполнено {done} из {total} ({pct}%)",
        "week_ai": "🤖 ИИ нашёл в чатах: {items}", "week_next": "<b>Следующая неделя</b>", "week_empty": "Задач на неделе не было.",
        "week_next_empty": "На следующей неделе пока пусто — отличное время для важного.",
        "inline_add": "➕ Добавить себе", "inline_open": "🤖 Открыть бота", "inline_added": "✅ Добавлено в твой календарь",
        "inline_already": "Это уже есть в твоём календаре", "inline_gone": "Задача больше не существует",
        "inline_start": "Сначала открой бота и нажми «Старт», потом нажми кнопку ещё раз",
        "inline_empty": "Нет подходящих задач — открыть бота", "inline_hint": "Нажми, чтобы отправить карточку",
        "short_desc": "Собираю дела из чатов, голосовых и скриншотов в календарь и напоминаю вовремя. ИИ-ассистент в Telegram.",
        "desc": (
            "Забываешь договорённости в куче чатов? Я помогу.\n\n"
            "📨 Перешли сообщения, надиктуй голосовое или пришли скриншот — найду встречи, дедлайны и дела\n"
            "🗓 Сложу всё в календарь: прямо в Telegram и в календаре телефона\n"
            "✨ Спланирую день и подведу итоги недели\n"
            "💬 Спроси: «что у меня завтра?» или «перенеси встречу на субботу»\n\n"
            "Нажми «Старт» 👇"
        ),
        "cmd_today": "Задачи на сегодня", "cmd_tasks": "Ближайшие задачи", "cmd_plan": "ИИ спланирует день",
        "cmd_week": "Продуктивность и обзор недели", "cmd_settings": "Напоминания, язык, утро и вечер",
        "cmd_calendar": "Задачи в календаре телефона", "cmd_help": "Что умеет бот",
        "cmd_task": "Ответом на сообщение, голосовое или фото — сделать задачу", "cmd_group_help": "Как я работаю в чате",
    },
    "en": {
        "menu": "Calendar", "cal_name": "Tasks from Telegram",
        "today": "Today", "tomorrow": "Tomorrow", "all_day": "all day", "starts_now": "Starting now",
        "cat_work": "Work", "cat_personal": "Personal", "cat_shopping": "Shopping", "cat_health": "Health",
        "cat_study": "Study", "cat_other": "Other", "prio_high": "🔥 Important", "prio_low": "Not urgent",
        "rep_daily": "every day", "rep_weekdays": "on weekdays", "rep_weekly": "every week", "rep_monthly": "every month",
        "help": (
            "<b>Hi{name}! I collect your tasks from chats.</b>\n\n"
            "📨 Forward me messages — even a batch at once, I'll read the whole conversation\n"
            "🎙 Send a voice note: “dentist tomorrow at 10”\n"
            "📸 Send a chat screenshot, a poster or a ticket\n"
            "👥 Add me to a work chat — I'll catch new tasks and reschedules\n"
            "↗️ Type @{bot} in any chat to share a meeting\n\n"
            "<b>Just ask</b>\n"
            "• “what do I have tomorrow?”, “when am I free on Thursday?”\n"
            "• “move the meeting with Mike to Saturday”, “cancel the dentist”\n"
            "• “stand-up every Tuesday at 10”, “buy cake, balloons, candles for the party”\n\n"
            "⏰ I'll remind you on time, ☀️ send a morning plan, 🌙 an evening wrap-up and 📊 a weekly review on Sundays\n\n"
            "<b>Commands</b>\n"
            "/today — today's tasks\n/tasks — upcoming tasks\n/plan — let AI plan your day\n"
            "/week — weekly productivity and review\n/settings — reminders, language, morning and evening\n"
            "/calendar — show tasks in your phone calendar\n/help — this help"
        ),
        "settings_text": "Reminders, language, morning plan and evening wrap-up — all in settings:",
        "btn_settings": "⚙️ Open settings",
        "no_public_url": "Unavailable: PUBLIC_URL is not set.",
        "calendar_text": "Connect your calendar once — confirmed tasks will appear there automatically.\n\nOther calendar apps — subscribe by link:\n{url}",
        "today_empty": "Nothing for today 🎉", "tasks_empty": "No tasks.",
        "unknown_cmd": "Unknown command. See /help",
        "doc_unsupported": "Send text, a voice note, a photo or a screenshot — I can't read other files.",
        "ai_failed": "Couldn't process that, please try again later.",
        "nothing_found": "No tasks found here. You can ask: “what do I have tomorrow?” or “move the meeting to Saturday”.",
        "group_help": "I notice meetings, deadlines and reschedules in this chat and send them privately to those who started me. I don't post in the chat.\n\n🎙 I don't process voice notes and photos on my own. Reply to one with /task and I'll send you the task privately.",
        "private_only": "This command works in a private chat — your tasks stay private there.",
        "task_need_reply": "Reply with /task to a message, voice note or photo and I'll turn it into a task.",
        "start_first": "First press “Start” in a private chat with me — the task will arrive there.",
        "nothing_here": "No task found here.",
        "group_welcome": "👋 Hi! I notice meetings, deadlines and reschedules in this chat and send them privately to those who started me. I stay quiet here.\n\nTo get tasks from this chat, press the button below and “Start”.",
        "group_privacy": "⚠️ I can't see chat messages right now. Make me an admin, or disable privacy mode in @BotFather (/setprivacy → Disable) and add me again.",
        "group_voice": "🎙 Voice note or photo — reply to it with /task.",
        "btn_get_tasks": "🚀 Get tasks privately",
        "handled": "Already handled", "saved": "Saved",
        "btn_add": "✅ Add", "btn_apply": "✅ Apply", "btn_edit": "✏️ Edit and set reminders",
        "btn_gcal": "📅 Add to Google Calendar", "btn_open": "🗓 Open calendar", "btn_done": "✅ Done",
        "btn_all_tomorrow": "📅 All to tomorrow", "btn_all_done": "✅ All done", "btn_review": "🗓 Review in calendar",
        "btn_plan": "✨ Plan my day", "btn_iphone": "🍎 iPhone / Mac", "btn_google": "📅 Google Calendar",
        "remind": "🔔 Reminders: {list}", "no_remind": "🔕 No reminders", "repeat": "🔁 Repeats {label}",
        "conflict": "⚠️ Overlaps with: {list}",
        "ch_delete": "🗑 <b>Delete this task?</b>", "ch_edit": "✏️ <b>Change “{title}”?</b>", "ch_done": "✅ Mark as done",
        "not_actual": "No longer relevant", "kept": "Kept as is", "deleted": "🗑 Deleted: <b>{title}</b>",
        "updated": "✅ <b>Updated</b>", "applied": "Done",
        "task_gone": "Task was deleted", "great": "Great!", "done_header": "✅ <b>Done</b>", "remind_at": "I'll remind you at {time}",
        "reminder": "Reminder", "remind_again": "Reminding you again",
        "moved": "📅 Moved to tomorrow: {items}", "marked": "✅ Marked as done: {items}",
        "evening": "🌙 <b>Not done today: {items}</b>", "digest": "☀️ <b>Good morning! Today's plan:</b>",
        "plan_header": "✨ <b>Day plan · {day}</b>", "plan_nothing": "Nothing to plan: no untimed tasks that day.",
        "plan_failed": "Couldn't build a plan, please try later.", "plan_applied": "✅ Plan applied",
        "week_header": "📊 <b>Week in review {range}</b>", "week_done": "✅ Done {done} of {total} ({pct}%)",
        "week_ai": "🤖 Found in chats by AI: {items}", "week_next": "<b>Next week</b>", "week_empty": "No tasks this week.",
        "week_next_empty": "Next week is still free — a great time for something important.",
        "inline_add": "➕ Add to my calendar", "inline_open": "🤖 Open the bot", "inline_added": "✅ Added to your calendar",
        "inline_already": "It's already in your calendar", "inline_gone": "This task no longer exists",
        "inline_start": "Open the bot and press “Start” first, then press the button again",
        "inline_empty": "No matching tasks — open the bot", "inline_hint": "Tap to send the card",
        "short_desc": "I turn chats, voice notes and screenshots into calendar tasks and remind you on time. AI assistant in Telegram.",
        "desc": (
            "Losing track of plans across dozens of chats? I can help.\n\n"
            "📨 Forward messages, send a voice note or a screenshot — I'll find meetings, deadlines and tasks\n"
            "🗓 I'll put them in a calendar: right in Telegram and in your phone calendar\n"
            "✨ I'll plan your day and review your week\n"
            "💬 Ask: “what do I have tomorrow?” or “move the meeting to Saturday”\n\n"
            "Press “Start” 👇"
        ),
        "cmd_today": "Today's tasks", "cmd_tasks": "Upcoming tasks", "cmd_plan": "Let AI plan my day",
        "cmd_week": "Weekly productivity and review", "cmd_settings": "Reminders, language, morning and evening",
        "cmd_calendar": "Tasks in your phone calendar", "cmd_help": "What the bot can do",
        "cmd_task": "Reply to a message, voice note or photo to make a task", "cmd_group_help": "How I work in this chat",
    },
    "ky": {
        "menu": "Календарь", "cal_name": "Telegram'дагы иштер",
        "today": "Бүгүн", "tomorrow": "Эртең", "all_day": "күн бою", "starts_now": "Азыр башталат",
        "cat_work": "Жумуш", "cat_personal": "Жеке", "cat_shopping": "Сатып алуу", "cat_health": "Ден соолук",
        "cat_study": "Окуу", "cat_other": "Башка", "prio_high": "🔥 Маанилүү", "prio_low": "Шашылыш эмес",
        "rep_daily": "күн сайын", "rep_weekdays": "иш күндөрү", "rep_weekly": "жума сайын", "rep_monthly": "ай сайын",
        "help": (
            "<b>Салам{name}! Мен чаттардагы иштериңизди чогултам.</b>\n\n"
            "📨 Билдирүүлөрдү мага жөнөтүңүз — бир нечесин чогуу жөнөтсөңүз да, бүт сүйлөшүүнү түшүнөм\n"
            "🎙 Үн билдирүү жазыңыз: «эртең саат 10до тиш доктур»\n"
            "📸 Сүйлөшүүнүн скриншотун, афишаны же билетти жөнөтүңүз\n"
            "👥 Мени жумуш чатына кошуңуз — жаңы иштерди жана жылдырууларды байкайм\n"
            "↗️ Каалаган чатта @{bot} деп жазып, жолугушууну бөлүшүңүз\n\n"
            "<b>Жөн эле жазыңыз</b>\n"
            "• «эртең менде эмне бар?», «бейшембиде качан бошмун?»\n"
            "• «Миша менен жолугушууну ишембиге жылдыр», «тиш доктурду жокко чыгар»\n"
            "• «ар шейшембиде саат 10до планёрка», «туулган күнгө торт, шар, шам сатып ал»\n\n"
            "⏰ Убагында эскертем, ☀️ эртең менен пландын, 🌙 кечинде аткарылбагандарды, 📊 жекшембиде жуманын жыйынтыгын жөнөтөм\n\n"
            "<b>Буйруктар</b>\n"
            "/today — бүгүнкү иштер\n/tasks — жакынкы иштер\n/plan — ЖИ күнүңүздү пландайт\n"
            "/week — жуманын натыйжалуулугу жана сереби\n/settings — эскертмелер, тил, эртең жана кеч\n"
            "/calendar — иштерди телефондун календарында көрсөтүү\n/help — ушул жардам"
        ),
        "settings_text": "Эскертмелер, тил, эртең мененки план жана кечки жыйынтык — баары жөндөөлөрдө:",
        "btn_settings": "⚙️ Жөндөөлөрдү ачуу",
        "no_public_url": "Жеткиликсиз: PUBLIC_URL көрсөтүлгөн эмес.",
        "calendar_text": "Календарды бир жолу туташтырыңыз — ырасталган иштер ага өзү эле түшөт.\n\nБашка календарь — шилтеме аркылуу кошуңуз:\n{url}",
        "today_empty": "Бүгүнгө иш жок 🎉", "tasks_empty": "Иш жок.",
        "unknown_cmd": "Мындай буйрукту билбейм. Тизме — /help",
        "doc_unsupported": "Текст, үн билдирүү, сүрөт же скриншот жөнөтүңүз — башка файлдарды окубайм.",
        "ai_failed": "Түшүнө алган жокмун, кийинчерээк кайталаңыз.",
        "nothing_found": "Бул жерден иш тапкан жокмун. Мындай сурасаңыз болот: «эртең менде эмне бар?» же «жолугушууну ишембиге жылдыр».",
        "group_help": "Мен бул чаттагы жолугушууларды, мөөнөттөрдү жана жылдырууларды байкап, мени иштеткендерге жеке жөнөтөм. Чатка эч нерсе жазбайм.\n\n🎙 Үн билдирүүлөрдү жана сүрөттөрдү өзүм талдабайм. Ага /task буйругу менен жооп бериңиз — тапшырманы сизге жеке жөнөтөм.",
        "private_only": "Бул буйрук жеке чатта иштейт — ал жерде иштериңизди башкалар көрбөйт.",
        "task_need_reply": "Билдирүүгө, үн билдирүүгө же сүрөткө /task буйругу менен жооп бериңиз — андан тапшырма жасайм.",
        "start_first": "Адегенде мага жеке чатта «Старт» басыңыз — тапшырма ошол жакка келет.",
        "nothing_here": "Бул жерден иш тапкан жокмун.",
        "group_welcome": "👋 Салам! Мен бул чаттагы жолугушууларды, мөөнөттөрдү жана жылдырууларды байкап, мени иштеткендерге жеке жөнөтөм. Бул жерде унчукпайм.\n\nБул чаттан тапшырмаларды алуу үчүн төмөнкү баскычты жана «Старт» басыңыз.",
        "group_privacy": "⚠️ Азыр чаттын билдирүүлөрүн көрбөйм. Мени администратор кылыңыз же @BotFather'де купуялуулук режимин өчүрүңүз (/setprivacy → Disable) жана кайра кошуңуз.",
        "group_voice": "🎙 Үн билдирүү же сүрөт — ага /task буйругу менен жооп бериңиз.",
        "btn_get_tasks": "🚀 Тапшырмаларды жеке алуу",
        "handled": "Буга чейин иштетилген", "saved": "Сакталды",
        "btn_add": "✅ Кошуу", "btn_apply": "✅ Колдонуу", "btn_edit": "✏️ Өзгөртүү жана эскертмелер",
        "btn_gcal": "📅 Google Календарга", "btn_open": "🗓 Календарды ачуу", "btn_done": "✅ Аткарылды",
        "btn_all_tomorrow": "📅 Баарын эртеңге", "btn_all_done": "✅ Баары аткарылды", "btn_review": "🗓 Календардан карап чыгуу",
        "btn_plan": "✨ Күндү пландоо", "btn_iphone": "🍎 iPhone / Mac", "btn_google": "📅 Google Календарь",
        "remind": "🔔 Эскертем: {list}", "no_remind": "🔕 Эскертмесиз", "repeat": "🔁 Кайталанат: {label}",
        "conflict": "⚠️ Бул убакытта буга чейин: {list}",
        "ch_delete": "🗑 <b>Тапшырманы өчүрөсүзбү?</b>", "ch_edit": "✏️ <b>«{title}» өзгөртөсүзбү?</b>", "ch_done": "✅ Аткарылды деп белгилөө",
        "not_actual": "Эми актуалдуу эмес", "kept": "Өзгөрүүсүз калтырдым", "deleted": "🗑 Өчүрүлдү: <b>{title}</b>",
        "updated": "✅ <b>Жаңыртылды</b>", "applied": "Даяр",
        "task_gone": "Тапшырма өчүрүлгөн", "great": "Сонун!", "done_header": "✅ <b>Аткарылды</b>", "remind_at": "Саат {time} эскертем",
        "reminder": "Эскертме", "remind_again": "Дагы бир жолу эскертем",
        "moved": "📅 Эртеңге жылдырылды: {items}", "marked": "✅ Аткарылды деп белгиленди: {items}",
        "evening": "🌙 <b>Бүгүн белгиленбеген: {items}</b>", "digest": "☀️ <b>Кутман таң! Бүгүнкү план:</b>",
        "plan_header": "✨ <b>Күндүн планы · {day}</b>", "plan_nothing": "Пландай турган эч нерсе жок: бул күнү убакытсыз иш жок.",
        "plan_failed": "План түзө алган жокмун, кийинчерээк кайталаңыз.", "plan_applied": "✅ План колдонулду",
        "week_header": "📊 <b>Жуманын жыйынтыгы {range}</b>", "week_done": "✅ {total} ичинен {done} аткарылды ({pct}%)",
        "week_ai": "🤖 ЖИ чаттардан тапты: {items}", "week_next": "<b>Кийинки жума</b>", "week_empty": "Бул жумада тапшырма болгон жок.",
        "week_next_empty": "Кийинки жума азырынча бош — маанилүү иш үчүн жакшы убакыт.",
        "inline_add": "➕ Өзүмө кошуу", "inline_open": "🤖 Ботту ачуу", "inline_added": "✅ Календарыңызга кошулду",
        "inline_already": "Бул календарыңызда бар", "inline_gone": "Бул тапшырма жок болуп калган",
        "inline_start": "Адегенде ботту ачып, «Старт» басыңыз, анан баскычты кайра басыңыз",
        "inline_empty": "Ылайыктуу тапшырма жок — ботту ачуу", "inline_hint": "Карточканы жөнөтүү үчүн басыңыз",
        "short_desc": "Чаттардагы, үн билдирүүлөрдөгү жана скриншоттордогу иштерди календарга чогултуп, убагында эскертем. ЖИ-жардамчы.",
        "desc": (
            "Көп чаттардагы макулдашууларды унутуп жатасызбы? Мен жардам берем.\n\n"
            "📨 Билдирүүлөрдү жөнөтүңүз, үн билдирүү жазыңыз же скриншот жибериңиз — жолугушууларды, мөөнөттөрдү жана иштерди табам\n"
            "🗓 Баарын календарга салам: Telegram'дын ичинде жана телефондун календарында\n"
            "✨ Күнүңүздү пландап, жуманы жыйынтыктайм\n"
            "💬 Сураңыз: «эртең менде эмне бар?» же «жолугушууну ишембиге жылдыр»\n\n"
            "«Старт» басыңыз 👇"
        ),
        "cmd_today": "Бүгүнкү иштер", "cmd_tasks": "Жакынкы иштер", "cmd_plan": "ЖИ күнүңүздү пландайт",
        "cmd_week": "Жуманын натыйжалуулугу жана сереби", "cmd_settings": "Эскертмелер, тил, эртең жана кеч",
        "cmd_calendar": "Телефондун календарындагы иштер", "cmd_help": "Бот эмне кыла алат",
        "cmd_task": "Билдирүүгө, үнгө же сүрөткө жооп берип тапшырма жасоо", "cmd_group_help": "Чатта кантип иштейм",
    },
}


def t(lang: str, key: str, **kw) -> str:
    text = T.get(lang, T["ru"]).get(key) or T["ru"][key]
    return text.format(**kw) if kw else text
