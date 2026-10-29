"""Quick words that may be about «Дела» (gymbot.services.life): tasks, study, the schedule, deadlines, notes,
house chores, projects. A cheap filter before the «Дела» model in handlers/log_text.process_text: a match only
means "ask the model"; the model returning no action sends the message on its usual way (food, a workout, a
program edit). So the list leans to catching more, except for words that are everyday in a gym diary
(«пару подходов», «позанимался в зале», «купил протеин»), which would cost a model call on every record.
"""

from __future__ import annotations

import re

_W = r"(?<![а-яёa-z])"  # no letter before
_E = r"(?![а-яёa-z])"  # no letter after

_COMMAND = re.compile(
    "|".join([
        # tasks
        r"задач", r"туду", r"todo", _W + r"в\s+дела" + _E, r"список\s+дел",
        # study and the schedule
        r"уч[её]б", r"позаниматьс", r"дедлайн", r"экзамен", r"зач[её]т", _W + r"сдать" + _E, r"расписани",
        r"запланир", _W + r"в\s+план(?:ы|ах)" + _E, _W + r"урок",
        _W + r"пар[аы]" + _E, _W + r"(?:на|после|перед|между|до)\s+пар(?:ой|ами|ах|у|ы)?" + _E,
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


def is_life_command(text: str) -> bool:
    """Whether `text` may be about «Дела»; the model decides (no action = not ours)."""
    return bool(_COMMAND.search(text))
