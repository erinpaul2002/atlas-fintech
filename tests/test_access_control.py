import pytest

from atlas.bot import handlers
from atlas.bot.inbound import Inbound
from atlas.config import settings
from atlas.db.models import User


class Reply:
    def __init__(self):
        self.messages = []

    async def finish(self, text):
        self.messages.append(text)
        return text


@pytest.fixture(autouse=True)
def isolated_access_settings(monkeypatch):
    monkeypatch.setattr(settings, "allowed_telegram_users", "")
    monkeypatch.setattr(settings, "allowed_chat_ids", "")
    monkeypatch.setattr(settings, "tester_passcode", "")


def inbound(user_id=7, chat_id=42, text="hello"):
    return Inbound(
        chat_id=chat_id,
        telegram_user_id=user_id,
        first_name="Evaluator",
        text=text,
    )


@pytest.mark.asyncio
async def test_gate_is_open_when_no_security_setting_exists():
    reply = Reply()

    allowed = await handlers._access_allowed(inbound(), User(), reply)

    assert allowed is True
    assert reply.messages == []


@pytest.mark.asyncio
async def test_user_and_chat_allowlists_bypass_the_passcode(monkeypatch):
    monkeypatch.setattr(settings, "allowed_telegram_users", "7, 8")
    monkeypatch.setattr(settings, "allowed_chat_ids", "-100123")
    monkeypatch.setattr(settings, "tester_passcode", "secret")

    assert await handlers._access_allowed(inbound(user_id=7), User(), Reply()) is True
    assert await handlers._access_allowed(
        inbound(user_id=99, chat_id=-100123), User(), Reply()
    ) is True


@pytest.mark.asyncio
async def test_correct_passcode_grants_only_the_sender(monkeypatch):
    granted = []
    reply = Reply()
    monkeypatch.setattr(settings, "tester_passcode", "secret")

    async def authorize(user_id, telegram_user_id):
        granted.append((user_id, telegram_user_id))

    monkeypatch.setattr(handlers.users_repo, "authorize_user", authorize)
    user = User(_id="mongo-user", telegram_chat_id=-100123)

    allowed = await handlers._access_allowed(
        inbound(user_id=77, chat_id=-100123, text=" secret "), user, reply
    )

    assert allowed is False
    assert granted == [("mongo-user", 77)]
    assert reply.messages == [handlers.ACCESS_GRANTED]


@pytest.mark.asyncio
async def test_persisted_sender_grant_bypasses_the_gate(monkeypatch):
    monkeypatch.setattr(settings, "tester_passcode", "secret")
    user = User(authorized_telegram_user_ids=[77])

    assert await handlers._access_allowed(inbound(user_id=77), user, Reply()) is True
    reply = Reply()
    assert await handlers._access_allowed(inbound(user_id=78), user, reply) is False
    assert reply.messages == [handlers.ACCESS_RESTRICTED]


@pytest.mark.asyncio
async def test_unauthorized_turn_stops_before_history_or_llm_work(monkeypatch):
    monkeypatch.setattr(settings, "tester_passcode", "secret")
    user = User(_id="mongo-user")

    async def get_or_create(*args):
        return user

    async def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized messages must not reach the turn pipeline")

    monkeypatch.setattr(handlers.users_repo, "get_or_create", get_or_create)
    monkeypatch.setattr(handlers.messages_repo, "has_history", forbidden)
    monkeypatch.setattr(handlers.loop, "run_turn", forbidden)
    reply = Reply()

    await handlers._handle(inbound(text="wrong"), reply)

    assert reply.messages == [handlers.ACCESS_RESTRICTED]
