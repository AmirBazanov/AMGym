"""Bot texts for the README chat illustrations, produced by the bot's own formatting code on the seeded DB."""

import asyncio
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gymbot.config import get_settings
from gymbot.db.models import User, Workout, WorkoutSet
from gymbot.db.session import make_engine
from gymbot.handlers import chat_settings as hcs
from gymbot.handlers import products as hp
from gymbot.handlers.log_text import keyboard, render_preview
from gymbot.llm.schemas import ParsedExercise, ParsedFood, ParsedSet, ParseResult
from gymbot.services import answer_direct as direct
from gymbot.services import chat_settings as cs
from gymbot.services import records
from gymbot.services.answer_intent import plan_question
from gymbot.services.products import ProductInfo

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 8, 18, 30, tzinfo=TZ).astimezone(UTC)


def kb_rows(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard] if markup else []


async def main() -> None:
    settings = get_settings()
    engine, sm = make_engine(settings.database_url)
    out = {}
    async with sm() as s:
        user = await s.scalar(select(User))

        # (a) food from text
        food = ParseResult(
            kind="food",
            foods=[
                ParsedFood(description="плов", grams=300, kcal=540, protein_g=18, fat_g=24, carbs_g=63),
                ParsedFood(description="касушка (пиала) чая с молоком", grams=250, kcal=95, protein_g=4, fat_g=4, carbs_g=11),
                ParsedFood(description="лепёшка, половина", grams=125, kcal=330, protein_g=11, fat_g=3, carbs_g=66),
            ],
            note="Лепёшку посчитал по твоему факту: целая ~250 г.",
        )
        text = "плов, касушка и пол лепёшки"
        out["food"] = {"user": text, "bot": render_preview(food, text), "buttons": kb_rows(keyboard("t", record=True, fact=False))}

        # (b) barcode product card
        p = ProductInfo(name="Протеиновый батончик арахис-карамель", brand=None, kcal=355, protein_g=33,
                        fat_g=12, carbs_g=30, net_weight_g=60, barcode="4600000000000", source="off")
        card = hp.Card(user_id=1, product=p, raw_text="[photo]", sent_at=NOW, at=NOW)
        out["barcode"] = {"bot": hp.card_text(card), "buttons": kb_rows(hp.amount_keyboard("t", p))}

        # (c) the plan for tomorrow, straight from the diary
        q = "какая завтра тренировка и какие веса"
        pq = plan_question(q)
        out["plan"] = {"user": q, "bot": await direct.plan_reply(s, user, settings, q, pq, TZ, NOW), "pq": repr(pq)}

        # (d) a saved workout and its records (the 07.10 sets)
        wid = await s.scalar(select(Workout.id).where(Workout.performed_on == date(2026, 10, 7)))
        set_ids = list(await s.scalars(select(WorkoutSet.id).where(WorkoutSet.workout_id == wid)))
        found = await records.find_new(s, user.id, set_ids)
        wtext = "супинация 20 на 8 три подхода, француз в блоке 60 на 10 три подхода, пек дек 50 на 10 два"
        workout = ParseResult(kind="workout", exercises=[
            ParsedExercise(exercise="сгибания с гантелями на бицепс с супинацией", sets=[ParsedSet(reps=8, weight_kg=20)] * 3),
            ParsedExercise(exercise="французский жим в блоке из-за головы", sets=[ParsedSet(reps=10, weight_kg=60)] * 3),
            ParsedExercise(exercise="отведения пек дек на заднюю дельту", sets=[ParsedSet(reps=10, weight_kg=50)] * 2),
        ])
        out["record"] = {"user": wtext, "preview": render_preview(workout, wtext),
                         "saved": render_preview(workout).removeprefix("Записать?\n")
                         + "\n\nСохранено ✅ Видно в дневнике, /undo — отменить.",
                         "records": records.message_text(found)}

        # (e) settings from the chat: the norm before the command is 2500 / 150
        user.kcal_target, user.protein_target_g = 2500, 150
        snap = await cs.load_snapshot(s, user, NOW.astimezone(TZ).date())
        actions, _ = cs.parse_actions({"actions": [{"type": "targets", "kcal": 2800, "protein": 170}]})
        plan = await cs.resolve(s, snap, actions)
        out["settings"] = {"user": "норма 2800 ккал, белок 170", "bot": hcs.render(plan),
                           "buttons": kb_rows(hcs.keyboard("t"))}
        await s.rollback()
    await engine.dispose()
    print(json.dumps(out, ensure_ascii=False, indent=1))


asyncio.run(main())
