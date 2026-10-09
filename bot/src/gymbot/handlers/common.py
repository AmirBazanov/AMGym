from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo

from gymbot.config import Settings
from gymbot.db.session import Sessionmaker
from gymbot.services import live, tg_html
from gymbot.services.programs import find_day, format_item, load_program, program_position
from gymbot.services.users import active_program, get_or_create_user
from gymbot.services.workouts import delete_last_chat_sets

router = Router(name="common")

WEEKDAYS = ["", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

HELP = (
    "Пиши тренировку обычным текстом, я разберу её и попрошу подтвердить:\n"
    "• «жим лёжа 3 по 10 на 60»\n"
    "• «сгибания 3х12 по 14, французский жим 3 по 10 на 25»\n"
    "• «дропсет на бицепс 12-6-6 с 16 кг»\n"
    "Еду тоже можно: «200 г гречки и 2 яйца».\n"
    "Можно голосом: пришли голосовое до 2 минут, я покажу, что распознал.\n"
    "Можно фото: тарелку оценю, а по штрихкоду или этикетке упаковки запишу точно и запомню продукт.\n"
    "Сохранённое правится так же, с подтверждением: «удали самсу», «самса была 2, а не 3», "
    "«удали последний подход», «в жиме было 85, а не 80», «сон был 7 часов, а не 5».\n"
    "Программу тоже можно править текстом: «убери французский жим из дня рук», «поставь на сгибания 30 кг».\n\n"
    "/today — план на сегодня по программе\n"
    "/plan — план на сегодня с поправками под сон, боли, питание и восстановление\n"
    "/undo — удалить последнюю запись из чата\n"
    "/advice — советы по питанию, тренировкам и восстановлению (или напиши «что посоветуешь?»)\n"
    "/facts — что я помню о тебе; добавить: «запомни: не ем творог»\n"
    "/products — мои продукты с упаковок; записать снова: «тот же батончик», «протеин 1 скуп»\n"
    "/backup — копия базы в этот чат (каждый день приходит сама)\n"
    "/llm — нейросети: состояние и расход; /llm test — проверить первую\n"
    "Дневник с программой, историей и графиками — кнопка «Дневник» внизу."
)


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, settings: Settings) -> None:
    url = settings.miniapp_url
    kb = (
        ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Открыть дневник", web_app=WebAppInfo(url=url))]],
            resize_keyboard=True,
        )
        if url
        else ReplyKeyboardRemove()
    )
    text = HELP
    if not settings.allowed_user_ids and message.from_user:
        text += f"\n\nБот сейчас открыт для всех. Впиши свой id {message.from_user.id} в ALLOWED_USER_IDS в .env."
    await message.answer(text, reply_markup=kb)


@router.message(Command("today"))
async def today(message: Message, settings: Settings, sessionmaker: Sessionmaker) -> None:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz).date()
    async with sessionmaker() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.full_name)  # type: ignore[union-attr]
        up = await active_program(session, user, now)
        program = await load_program(session, up.program_id)
        await session.commit()
    pos = program_position(up.started_on, len(program.weeks), now)
    if pos.not_started:
        await message.answer(f"Программа «{program.name}» начнётся {up.started_on:%d.%m}.")
        return
    if pos.finished:
        await message.answer(f"Программа «{program.name}» пройдена. Выбери новую дату старта в дневнике.")
        return
    day = find_day(program, pos.week, pos.weekday)
    if day is None:
        week = next(w for w in program.weeks if w.number == pos.week)
        days = ", ".join(WEEKDAYS[d.weekday] for d in week.days)
        await message.answer(f"Неделя {pos.week}, сегодня отдых. Тренировки на этой неделе: {days}.")
        return
    items = sorted(day.items, key=lambda i: i.order)
    text = "\n".join([f"Неделя {pos.week}, {WEEKDAYS[pos.weekday]}:"]
                     + [f"{item.order}. {item.exercise.name} — {format_item(item)}" for item in items])
    html = "\n\n".join(
        [f"🏋️ <b>Неделя {pos.week}, {WEEKDAYS[pos.weekday]}</b>"]
        + [tg_html.card_row(i, item.exercise.name, tg_html.escape(format_item(item))) for i, item in enumerate(items, 1)]
    )
    await tg_html.send_html(message.answer, html, text)


@router.message(Command("undo"))
async def undo(message: Message, sessionmaker: Sessionmaker) -> None:
    async with sessionmaker() as session:
        user = await get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
        removed = await delete_last_chat_sets(session, user)
        await session.commit()
    if removed:
        live.publish(user.id, "workouts", "state")
    await message.answer(f"Удалил подходов: {removed}." if removed else "Нечего удалять.")
