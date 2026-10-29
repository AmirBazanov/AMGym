"""Tasks and house chores of the dashboard from the chat: data.js, the state rules of app.js, the model's actions,
the /hub button, the preview and «Применить» (gymbot.services.hub_tasks, gymbot.handlers.hub), and the
dashboard's Telegram initData on /api/hub/state."""

from datetime import date

import httpx
import pytest
from conftest import init_data, make_settings
from test_log_text import T0, USER, FakeLLM, callback, message

from gymbot.api.app import create_app
from gymbot.handlers import hub as hh
from gymbot.handlers import log_text
from gymbot.services import hub, live
from gymbot.services import hub_tasks as ht
from gymbot.services.users import get_or_create_user

DATA_JS = """// Все данные сайта.
window.DATA = {
  owner: "Амир",
  projects: [ { name: 'AMGym', link: "https://github.com/x/y", desc: "бот // не комментарий [скобка]" } ],
  /* задачи по умолчанию */
  todos: [
    { id: "t1", text: "Обновить резюме (Backend & AI)", tag: "работа" },
    { id: 't2', text: 'Написать "отзыв" родителям', tag: "алгоритмика" }, // хвост
  ],
  chores: [
    { id: "c1", text: "Уборка", every: 7 },
    { id: "c4", text: "Вынести мусор", every: 2 },
  ]
};
"""
TODAY = date(2026, 10, 6)  # T0 in Europe/Moscow
SITE = ht.SiteLists(
    [{"id": "t1", "text": "Обновить резюме"}, {"id": "t2", "text": "Отзыв родителям"}],
    [{"id": "c1", "text": "Уборка", "every": 7}, {"id": "c4", "text": "Вынести мусор", "every": 2}],
)


@pytest.fixture
def hub_settings(tmp_path):
    site = tmp_path / "my-hub"
    site.mkdir()
    (site / "data.js").write_text(DATA_JS, encoding="utf-8")
    return make_settings(tmp_path, hub_dir=site, public_url="https://gym.example", hub_token="r", hub_write_token="w")


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(hh, "utcnow", lambda: T0)


@pytest.fixture(autouse=True)
def clean():
    hh.PENDING.clear()
    for store in (log_text.PENDING, log_text.CONTEXT, log_text.FACTS):
        store.clear()
    yield
    hh.PENDING.clear()


@pytest.fixture
def published(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(live, "publish", lambda *a: calls.append(a))
    return calls


async def owner(db, tg_id: int = USER) -> int:
    async with db() as s:
        user = await get_or_create_user(s, tg_id, "Amir")
        await s.commit()
        return user.id


async def stored(db, uid: int) -> dict:
    async with db() as s:
        return await hub.get_state(s, uid)


# ---- data.js and the app.js rules ----


def test_read_site(tmp_path):
    (tmp_path / "data.js").write_text(DATA_JS, encoding="utf-8")
    site = ht.read_site(tmp_path)
    assert [t["id"] for t in site.todos] == ["t1", "t2"]
    assert site.todos[1]["text"] == 'Написать "отзыв" родителям'
    assert site.chores == [{"id": "c1", "text": "Уборка", "every": 7}, {"id": "c4", "text": "Вынести мусор", "every": 2}]
    (tmp_path / "data.js").write_text("window.DATA = { todos: [ {id: 'x' ", encoding="utf-8")
    assert ht.read_site(tmp_path) == ht.SiteLists([], [])
    assert ht.read_site(tmp_path / "missing") == ht.SiteLists([], [])
    assert ht.read_site(None) == ht.SiteLists([], [])


def test_tasks_and_apply_mirror_app_js():
    state = {"done": {"t1": True}, "custom": [{"id": "u1", "text": "Билеты"}], "removed": ["t2"]}
    assert ht.tasks(state, SITE) == [
        {"id": "t1", "text": "Обновить резюме", "done": True}, {"id": "u1", "text": "Билеты", "done": False},
    ]
    actions = [
        ht.Action("add_task", id="u9", text="Позвонить маме"),
        ht.Action("add_task", id="u9", text="Позвонить маме"),  # applied twice: still one task
        ht.Action("reopen_task", id="t1"),
        ht.Action("complete_task", id="u1"),
        ht.Action("remove_task", id="u1"),  # a custom task leaves `custom`
        ht.Action("remove_task", id="t1"),  # a static one goes to `removed`
        ht.Action("chore_done", id="c4", day=TODAY),
    ]
    out = ht.apply(state, actions, SITE)
    assert out["custom"] == [{"id": "u9", "text": "Позвонить маме"}]
    assert out["removed"] == ["t2", "t1"]
    assert out["done"] == {"t1": False, "u1": True}
    assert out["chores"] == {"c4": "2026-10-06"}
    assert state["custom"] == [{"id": "u1", "text": "Билеты"}]  # the input is not touched
    assert ht.apply(out, actions, SITE) == out  # idempotent: the 409 retry applies them again


def test_chores_left():
    got = ht.chores({"chores": {"c1": "2026-10-01"}}, SITE, TODAY)
    assert [(c["id"], c["left"], c["last"]) for c in got] == [("c1", 2, "2026-10-01"), ("c4", 0, None)]


def test_parse_actions_validates_ids():
    ids = iter(["u100", "u101"])
    data = {"actions": [
        {"type": "add_task", "text": "  купить   билеты  "},
        {"type": "complete_task", "id": "t1"},
        {"type": "complete_task", "id": "t404"},
        {"type": "chore_done", "id": "c4", "date": "2026-10-05"},
        {"type": "chore_done", "id": "c1", "date": "2030-01-01"},  # the future is today
        {"type": "chore_done", "id": "c9"},
        {"type": "remove_all"},
        {"type": "add_task", "text": ""},
    ]}
    actions, notes = ht.parse_actions(data, {}, SITE, TODAY, lambda: next(ids))
    assert actions == [
        ht.Action("add_task", id="u100", text="Купить билеты"),
        ht.Action("complete_task", id="t1"),
        ht.Action("chore_done", id="c4", day=date(2026, 10, 5)),
        ht.Action("chore_done", id="c1", day=TODAY),
    ]
    assert notes == ["Не нашёл такое дело по дому.", "Не нашёл такую задачу."]
    assert ht.parse_actions({"actions": "x"}, {}, SITE, TODAY, str) == ([], [])
    assert ht.parse_actions(None, {}, SITE, TODAY, str) == ([], [])


@pytest.mark.parametrize("text", [
    "добавь задачу купить билеты", "в дела: позвонить маме", "закрой задачу про резюме", "вынес мусор",
    "сделал уборку", "убрался дома", "постирал вчера", "закупился продуктами", "оплатил коммуналку", "туду: код",
])
def test_command_words(text):
    assert ht.is_hub_command(text)


@pytest.mark.parametrize("text", [
    "съел гречку 200 г", "сделал жим 3×8 по 60", "убери французский жим из дня рук", "купил протеин",
    "оплатил абонемент", "продукты: курица 200 г", "мои продукты", "спал 6 часов", "убрал жим из программы",
])
def test_not_command_words(text):
    assert not ht.is_hub_command(text)


# ---- the bot ----


async def test_hub_command_button(hub_settings):
    msg = message("/hub")
    await hh.cmd_hub(msg, hub_settings)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert kb.inline_keyboard[0][0].web_app.url == "https://gym.example/hub/"
    msg = message("/hub")
    await hh.cmd_hub(msg, make_settings(hub_settings.hub_dir.parent / "x"))
    assert msg.answer.await_args.args[0] == hh.NOT_SET


def token_of(msg) -> str:
    kb = msg.answer.await_args.kwargs["reply_markup"]
    return kb.inline_keyboard[0][0].callback_data.split(":", 1)[1]


async def test_voice_text_to_preview_and_apply(hub_settings, db, published):
    uid = await owner(db)
    llm = FakeLLM(hub_settings)
    llm.answers.append({"actions": [
        {"type": "add_task", "text": "купить билеты"}, {"type": "chore_done", "id": "c4", "date": None},
    ]})
    msg = message("добавь задачу купить билеты и я вынес мусор")
    await log_text.process_text(msg, msg.text, hub_settings, db, llm.client, raw_text="[voice] …", prefix="Распознал\n\n")
    assert len(llm.bodies) == 1  # only the hub model, not the parser
    prompt = llm.bodies[0]["messages"][-1]["content"]
    assert "t1: Обновить резюме (Backend & AI)" in prompt and "c4: Вынести мусор" in prompt
    text = msg.answer.await_args.args[0]
    assert text.startswith("Распознал\n\nВ дела:\n")
    assert "• Добавлю задачу «Купить билеты»" in text and "• Отмечу «Вынести мусор» — сделано сегодня" in text
    assert (await stored(db, uid))["version"] == 0  # nothing before the button

    token = token_of(msg)
    cb = callback(f"happly:{token}")
    await hh.on_apply(cb, hub_settings, db)
    state = (await stored(db, uid))
    assert state["version"] == 1
    assert [t["text"] for t in state["state"]["custom"]] == ["Купить билеты"]
    assert state["state"]["chores"] == {"c4": "2026-10-06"}
    assert published == [(uid, "hub")]
    edited = cb.message.edit_text.await_args
    assert edited.args[0].startswith("Готово:\n")
    assert edited.kwargs["reply_markup"].inline_keyboard[0][0].web_app.url == "https://gym.example/hub/"

    again = callback(f"happly:{token}")  # a second tap adds nothing
    await hh.on_apply(again, hub_settings, db)
    assert again.answer.await_args.args[0] == hh.STALE
    assert (await stored(db, uid))["version"] == 1


async def test_apply_merges_a_save_in_between(hub_settings, db, published):
    uid = await owner(db)
    llm = FakeLLM(hub_settings)
    llm.answers.append({"actions": [{"type": "complete_task", "id": "t1"}]})
    msg = message("закрой задачу про резюме")
    await log_text.process_text(msg, msg.text, hub_settings, db, llm.client)
    async with db() as s:  # the phone saved meanwhile
        await hub.save_state(s, uid, 0, {"custom": [{"id": "u1", "text": "С телефона"}]}, T0)
    await hh.on_apply(callback(f"happly:{token_of(msg)}"), hub_settings, db)
    got = await stored(db, uid)
    assert got["version"] == 2
    assert got["state"] == {"custom": [{"id": "u1", "text": "С телефона"}], "done": {"t1": True}}


async def test_not_ours_goes_on(hub_settings, db, monkeypatch):
    async def parser_answer(message, text, result, *args):
        return result, None, None

    monkeypatch.setattr(log_text, "_diary_answer", parser_answer)
    await owner(db)
    llm = FakeLLM(hub_settings)
    llm.answers.append({"actions": []})  # the hub model: nothing for the dashboard
    llm.answers.append({"kind": "question", "answer": "ок"})  # the parser gets the text
    msg = message("купил продукты в магазине, что приготовить?")
    await log_text.process_text(msg, msg.text, hub_settings, db, llm.client)
    assert len(llm.bodies) == 2
    assert "Ты ведёшь список задач" not in llm.bodies[1]["messages"][0]["content"]  # the parser
    assert not hh.PENDING


async def test_only_the_owner(hub_settings, db):
    await owner(db, tg_id=1)  # someone else is the owner
    llm = FakeLLM(hub_settings)
    msg = message("добавь задачу купить билеты")
    assert await hh.stage(msg, msg.text, msg.text, hub_settings, db, llm.client) is None
    assert llm.bodies == []


async def test_model_failure_goes_on(hub_settings, db):
    await owner(db)
    llm = FakeLLM(hub_settings)
    llm.answers.append("not json at all")
    msg = message("добавь задачу купить билеты")
    assert await hh.stage(msg, msg.text, msg.text, hub_settings, db, llm.client) is None


async def test_drop(hub_settings, db):
    await owner(db)
    llm = FakeLLM(hub_settings)
    llm.answers.append({"actions": [{"type": "add_task", "text": "x"}]})
    msg = message("добавь задачу x")
    await log_text.process_text(msg, msg.text, hub_settings, db, llm.client)
    token = token_of(msg)
    await hh.on_drop(callback(f"hdrop:{token}"))
    assert token not in hh.PENDING


# ---- the dashboard inside Telegram: initData instead of the write token ----


async def test_state_with_init_data(hub_settings, db):
    await owner(db)
    app = create_app(hub_settings, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        mine = {"X-Telegram-Init-Data": init_data(USER)}
        r = await c.put("/api/hub/state", json={"base_version": 0, "state": {"done": {}}}, headers=mine)
        assert r.status_code == 200 and r.json()["version"] == 1
        assert (await c.get("/api/hub/state", headers=mine)).json()["version"] == 1
        other = {"X-Telegram-Init-Data": init_data(7)}
        assert (await c.get("/api/hub/state", headers=other)).status_code == 403
        forged = {"X-Telegram-Init-Data": init_data(USER, token="999:zzz")}
        assert (await c.get("/api/hub/state", headers=forged)).status_code == 403
        # initData decides on its own: a bad one is not rescued by the tokens
        r = await c.get("/api/hub/state?token=r", headers=forged | {"X-Hub-Write-Token": "w"})
        assert r.status_code == 403
        r = await c.options("/api/hub/state", headers={"Origin": "https://aventrallc.github.io",
                                                      "Access-Control-Request-Method": "GET"})
        assert "X-Telegram-Init-Data" in r.headers["access-control-allow-headers"]

