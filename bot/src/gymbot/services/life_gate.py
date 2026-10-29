"""Quick words that may be about «Дела» (gymbot.services.life): tasks, study, the schedule, deadlines, notes,
house chores, projects. A cheap filter before the «Дела» model in handlers/log_text.process_text: a match only
means "ask the model"; the model returning no action sends the message on its usual way (food, a workout, a
program edit). So the list leans to catching more, except for words that are everyday in a gym diary
(«пара яиц», «на пару кг», «урок бокса», «запланируй тренировку», «позанимался в зале», «купил протеин»), which
would cost a model call on every record. A match that also reads as a record is not asked at all
(handlers/log_text: the parser gets it first, and its kind="life" still reaches «Дела»).
"""

from __future__ import annotations

import re

_W = r"(?<![а-яёa-z])"  # no letter before
_E = r"(?![а-яёa-z])"  # no letter after

# Gym words: a message with them is a record or a program edit, never «Дела» by its weak words («запланируй
# тренировку ног», «хочу позаниматься в зале»). Also used by handlers/life (the note draft) and log_text.
GYM_WORDS = re.compile(
    r"жим|присед|тяг[аиуе]|подтяг|отжим|сгибан|разгибан|выпад|упражнен|трениров|программ|подход|гантел|штанг|"
    r"тренаж|(?<![а-яё])зал[еау]?(?![а-яё])|(?<![а-яё])(?:день|дня|дне)\s+(?:рук|ног|спин|груд|плеч)",
    re.IGNORECASE,
)
# The schedule's own words: «перенеси урок с понедельника на вторник» reads as a program edit too, but it is «Дела».
SCHEDULE_WORDS = re.compile(
    r"(?<![а-яё])(?:урок\w*|пар(?:а|ы|у|ой|ами|ах)?(?![а-яё])|расписани\w*|экзамен\w*|зач[её]т\w*|созвон\w*|"
    r"встреч\w*|лекци\w*|семинар\w*)",
    re.IGNORECASE,
)
_SPORT = re.compile(r"кардио|пробежк|растяжк|(?<![а-яё])(?:ноги|руки|спину|грудь|плечи)(?![а-яё])", re.IGNORECASE)
_WEEKDAY = r"(?:понедельник|вторник|сред[аеуы]|четверг|пятниц|суббот|воскресень)"

_COMMAND = re.compile(
    "|".join([
        # tasks
        r"задач", r"туду", r"todo", _W + r"в\s+дела" + _E, r"список\s+дел",
        # study and the schedule. Pairs only with a time, a day or a subject («пара в 10», «пары по вышмату»;
        # «расписание пар» is расписани): «пара яиц», «на пару кг», «после пары сходил в зал» are the diary.
        r"уч[её]б", r"дедлайн", r"экзамен", r"зач[её]т", _W + r"сдать" + _E, r"расписани",
        _W + r"в\s+план(?:ы|ах)" + _E,
        _W + r"пар[аы]\s+(?:(?:в|на)\s+(?:\d|сегодня|завтра|послезавтра|" + _WEEKDAY + r")|по\s+[а-яё])",
        # a lesson only at a time («урок в 10»; with a day of the week: is_life_command): «урок бокса 60 минут»
        _W + r"урок\w*\s+(?:в|на)\s+\d",
        # notes
        r"заметк", r"памятк", r"конспект",
        # house chores
        r"дела\s+по\s+дому", r"убрал(?:ся|ась)" + _E, r"уборк", r"стирк", r"постирал",
        r"вынес(?:ла)?\s+мусор", r"мусор\s+вынес", r"закупил(?:ся|ась)?\s+продукт", r"купил\s+продукты",
        r"продукты\s+купил", r"коммунал",
        # projects
        r"проект",
    ]),
    re.IGNORECASE,
)
# Weak words: «Дела» only without gym or sport words around («запланируй созвон», but «запланируй тренировку ног»).
_WEAK = re.compile(r"запланир|позаниматьс", re.IGNORECASE)
_LESSON_DAY = re.compile(_W + r"урок\w*" + _E + r".*" + _W + _WEEKDAY + r"|" + _W + _WEEKDAY + r".*" + _W + r"урок",
                         re.IGNORECASE | re.DOTALL)


def is_life_command(text: str) -> bool:
    """Whether `text` may be about «Дела»; the model decides (no action = not ours)."""
    if _COMMAND.search(text) or _LESSON_DAY.search(text):
        return True
    return bool(_WEAK.search(text)) and not (GYM_WORDS.search(text) or _SPORT.search(text))
