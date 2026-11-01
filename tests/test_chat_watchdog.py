"""The chat watchdog shuts the bot down when its chat subscription is lost."""

from __future__ import annotations

import asyncio

import pytest

from rlgymstream.chat import chatbot
from rlgymstream.chat.chatbot import RLGymStreamBot


class _StubBot:
    """Just enough of RLGymStreamBot for _chat_watchdog to run."""

    def __init__(self, health: list[bool]) -> None:
        # Checks return these values in order, then repeat the last one.
        self._health = list(health)
        self.chat_lost = False
        self.closed = False

    def _has_chat_subscription(self) -> bool:
        return self._health.pop(0) if len(self._health) > 1 else self._health[0]

    async def close(self) -> None:
        self.closed = True


def _run(stub: _StubBot, timeout: float = 2.0) -> None:
    async def main():
        await asyncio.wait_for(RLGymStreamBot._chat_watchdog(stub), timeout)
    asyncio.run(main())


@pytest.fixture(autouse=True)
def _fast_watchdog(monkeypatch):
    monkeypatch.setattr(chatbot, "_WATCHDOG_INTERVAL", 0)


def test_shuts_down_after_repeated_failures():
    stub = _StubBot([True, False, False, False])
    _run(stub)
    assert stub.chat_lost and stub.closed


def test_recovers_from_brief_outage():
    stub = _StubBot([True, False, False, True, False, False, False])
    _run(stub)
    # Only the second, sustained outage triggers a shutdown.
    assert stub.closed


def test_brief_outage_does_not_shut_down():
    stub = _StubBot([True, False, False, True])
    with pytest.raises(asyncio.TimeoutError):
        _run(stub, timeout=0.2)
    assert not stub.closed


def test_waits_for_first_subscription():
    """Before the first successful subscription (e.g. pending OAuth), never shut down."""
    stub = _StubBot([False] * 10 + [True, False, False, False])
    _run(stub)
    assert stub.closed  # only after it had been healthy once
