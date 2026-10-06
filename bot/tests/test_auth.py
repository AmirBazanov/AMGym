import json
import time

import pytest

from gymbot.api.auth import InitDataError, sign_init_data, validate_init_data

TOKEN = "123:abc"


def fields(**over):
    f = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 7, "first_name": "A", "last_name": "B"})}
    f.update(over)
    return f


def test_valid_init_data():
    u = validate_init_data(sign_init_data(fields(), TOKEN), TOKEN)
    assert u.id == 7 and u.name == "A B"


def test_wrong_token_rejected():
    with pytest.raises(InitDataError, match="bad hash"):
        validate_init_data(sign_init_data(fields(), "999:zzz"), TOKEN)


def test_tampered_hash_rejected():
    data = sign_init_data(fields(), TOKEN)
    head, h = data.rsplit("hash=", 1)
    bad = head + "hash=" + ("0" if h[0] != "0" else "1") + h[1:]
    with pytest.raises(InitDataError, match="bad hash"):
        validate_init_data(bad, TOKEN)


def test_tampered_payload_rejected():
    data = sign_init_data(fields(), TOKEN)
    forged = data.replace("%22id%22%3A+7", "%22id%22%3A+8")
    assert forged != data
    with pytest.raises(InitDataError):
        validate_init_data(forged, TOKEN)


def test_missing_hash_rejected():
    with pytest.raises(InitDataError, match="no hash"):
        validate_init_data("auth_date=1", TOKEN)


def test_expired_rejected():
    old = str(int(time.time()) - 8 * 24 * 3600)
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(sign_init_data(fields(auth_date=old), TOKEN), TOKEN)


def test_fresh_within_window_accepted():
    day_ago = str(int(time.time()) - 24 * 3600)
    assert validate_init_data(sign_init_data(fields(auth_date=day_ago), TOKEN), TOKEN).id == 7


def test_missing_user_rejected():
    f = fields()
    del f["user"]
    with pytest.raises(InitDataError, match="no user"):
        validate_init_data(sign_init_data(f, TOKEN), TOKEN)


def test_malformed_user_rejected():
    with pytest.raises(InitDataError, match="no user"):
        validate_init_data(sign_init_data(fields(user="not json"), TOKEN), TOKEN)
