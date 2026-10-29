"""Sign-in from a computer browser: the /web link and X-App-Token on the API (gymbot.services.web_login)."""

import time

import httpx
from aiogram.filters import CommandObject
from conftest import TOKEN, make_settings
from test_log_text import message

from gymbot.api.app import create_app
from gymbot.handlers import web_login as hw
from gymbot.services import live
from gymbot.services import web_login as wl

KEY = wl.signing_key(TOKEN)


def test_token_round_trip():
    token = wl.make_token(KEY, 42, now=1000)
    assert wl.verify(KEY, token, now=1000 + wl.TTL - 1) == 42
    assert wl.verify(KEY, token, now=1000 + wl.TTL + 1) is None  # expired
    assert wl.verify(wl.signing_key("other:token"), token, now=1000) is None  # another bot
    forged = token.replace("42.", "43.", 1)
    assert wl.verify(KEY, forged, now=1000) is None
    for junk in ("", "a.b.c", "42.1000", "42.x.y"):
        assert wl.verify(KEY, junk) is None
    # the live-stream key cannot sign it in
    assert wl.verify(KEY, live.make_token(live.signing_key(TOKEN), 42)) is None


async def test_api_takes_the_token(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42])
    app = create_app(settings, db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        mine = {"X-App-Token": wl.make_token(KEY, 42)}
        assert (await c.get("/api/state", headers=mine)).status_code == 200
        assert (await c.get("/api/life/view", headers=mine)).status_code == 200
        assert (await c.get("/api/life/view", headers={"X-App-Token": "42.1.bad"})).status_code == 401
        assert (await c.get("/api/life/view", headers={"X-App-Token": wl.make_token(KEY, 42, now=1)})).status_code == 401
        other = {"X-App-Token": wl.make_token(KEY, 7)}
        assert (await c.get("/api/life/view", headers=other)).status_code == 403
        assert (await c.get("/api/life/view")).status_code == 401


async def test_web_command(tmp_path):
    msg = message("/web")
    await hw.cmd_web(msg, make_settings(tmp_path, public_url="https://gym.example"), None)
    url = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].url
    assert url.startswith("https://gym.example/#login=")  # the fragment never reaches the server
    assert wl.verify(KEY, url.split("login=", 1)[1]) == msg.from_user.id
    msg = message("/web")
    await hw.cmd_web(msg, make_settings(tmp_path), None)
    assert msg.answer.await_args.args[0] == hw.NOT_SET


async def test_revoke_all_links(tmp_path, db):
    settings = make_settings(tmp_path, allowed_user_ids=[42])
    app = create_app(settings, db)
    old = {"X-App-Token": wl.make_token(KEY, 42, now=time.time() - 3600)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.get("/api/life/view", headers=old)).status_code == 200
        msg = message("/web выйти", user_id=42)
        await hw.cmd_web(msg, settings, db, CommandObject(command="web", args="выйти"))
        assert msg.answer.await_args.args[0] == hw.REVOKED
        assert (await c.get("/api/life/view", headers=old)).status_code == 401  # made before the revoke
        fresh = {"X-App-Token": wl.make_token(KEY, 42, now=time.time() + 5)}
        assert (await c.get("/api/life/view", headers=fresh)).status_code == 200
