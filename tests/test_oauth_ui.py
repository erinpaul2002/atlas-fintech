import pytest

from atlas.api import oauth
from atlas.api.oauth_ui import connected_page, expired_page


def _body(response) -> str:
    return response.body.decode("utf-8")


def test_connected_page_is_branded_and_actionable():
    response = connected_page()
    body = _body(response)

    assert response.status_code == 200
    assert "You&#x27;re connected." in body
    assert "ATLAS" in body
    assert "https://t.me/AtlasEPMBot" in body
    for service in ("Gmail", "Calendar", "Sheets", "Drive"):
        assert service in body
    assert response.headers["cache-control"] == "no-store"


def test_expired_page_uses_the_same_designed_shell():
    response = expired_page()
    body = _body(response)

    assert response.status_code == 400
    assert "This link timed out." in body
    assert 'data-tone="error"' in body


@pytest.mark.asyncio
async def test_successful_callback_cleans_authorization_code_from_url(monkeypatch):
    async def consume(state):
        return "user-1"

    async def exchange(code):
        return {"access_token": "token"}

    async def save(user_id, token):
        assert user_id == "user-1"
        assert token == {"access_token": "token"}

    monkeypatch.setattr(oauth.integrations, "consume_oauth_state", consume)
    monkeypatch.setattr(oauth.google, "exchange_code", exchange)
    monkeypatch.setattr(oauth.integrations, "save_google", save)

    response = await oauth.callback("valid-state-that-is-long-enough", code="one-time-code")

    assert response.status_code == 303
    assert response.headers["location"] == "/oauth/google/complete"
    assert response.headers["cache-control"] == "no-store"
