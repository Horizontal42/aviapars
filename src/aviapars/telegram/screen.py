from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

log = logging.getLogger(__name__)

# Where the anchor lives inside FSM state. Wizard steps read/write it through
# Screen only — no handler should touch this key directly.
SCREEN_KEY = "screen_message_id"

# Telegram assigns message_id sequentially per chat regardless of sender, so the gap between
# a typed command and the stored anchor is a reasonable proxy for "how many messages back is
# the screen". A scheduler notification burst (notify/notifier.py sends outside of Screen
# entirely) can bury the anchor far above the fold; editing it there is invisible without
# scrolling up — from the chat's bottom it looks like the bot ate the command and did nothing.
# Past this many messages back, a typed command gets a fresh message at the bottom instead.
_STALE_AFTER_MESSAGES = 3

_NOT_MODIFIED = "message is not modified"


class ScreenTransport(Protocol):
    """Structural subset of aiogram's Bot that Screen actually uses — same trick
    as notify/notifier.TelegramSender, and for the same reason: it keeps the
    edit/send/delete decisions testable without standing up a real Bot."""

    async def send_message(
        self, *, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
    ) -> Message: ...

    async def edit_message_text(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> object: ...

    async def delete_message(self, *, chat_id: int, message_id: int) -> bool: ...


class Screen:
    """The bot's one evolving message in a chat.

    Every wizard step used to call `message.answer()`, so creating a route left
    eight-plus separate messages in the chat. Telegram only lets a bot edit its
    own messages, never the user's, so "one screen" needs a tracked
    (chat_id, message_id) anchor that each step edits instead. The anchor is
    kept in FSM state so it survives between handlers, and re-persisted on every
    render.

    Two shapes of trigger converge here. A button tap already carries the bot's
    own message (`query.message`), which becomes the anchor directly — that also
    re-anchors the screen if the user scrolled back and tapped an older keyboard.
    A typed step carries the *user's* message instead; the anchor then comes from
    state, and the typed line is deleted (a bot may delete incoming messages in a
    private chat, per Bot API deleteMessage), which is what keeps a wizard that
    asks for typed input down to a single visible message.

    Deliberately not used for deal notifications (notify/notifier.py): those fire
    from the scheduler with no screen open and nothing to edit, and a price alert
    is a record worth keeping in the history rather than something to overwrite.
    """

    def __init__(
        self,
        bot: ScreenTransport,
        chat_id: int,
        state: FSMContext,
        *,
        anchor_id: int | None = None,
        input_message_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._state = state
        self._anchor_id = anchor_id
        self._input_message_id = input_message_id

    @property
    def anchor_id(self) -> int | None:
        return self._anchor_id

    async def show(self, text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.drop_input()
        if self._anchor_id is not None and await self._edit(text, reply_markup):
            await self._remember()
            return
        try:
            sent = await self._bot.send_message(chat_id=self._chat_id, text=text, reply_markup=reply_markup)
        except TelegramRetryAfter as exc:
            # A sibling of TelegramBadRequest, not a subclass — right after the scheduler
            # bursts deal notifications into this chat, Telegram's per-chat flood window
            # hits this call too. One retry after the wait is enough: the window is a
            # handful of seconds, and without this the exception went straight past here,
            # leaving drop_input()'s already-deleted user message with nothing to replace it.
            await asyncio.sleep(exc.retry_after)
            sent = await self._bot.send_message(chat_id=self._chat_id, text=text, reply_markup=reply_markup)
        self._anchor_id = sent.message_id
        await self._remember()

    async def drop_input(self) -> None:
        """Best-effort removal of the user's typed turn. If Telegram refuses
        (older than 48h, or a chat type where a bot may not), the typed line
        just stays visible — the bot's own side is still one message."""
        if self._input_message_id is None:
            return
        message_id, self._input_message_id = self._input_message_id, None
        try:
            await self._bot.delete_message(chat_id=self._chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            log.debug("could not delete the user's message: %s", exc)

    async def _edit(self, text: str, reply_markup: InlineKeyboardMarkup | None) -> bool:
        assert self._anchor_id is not None
        try:
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=self._anchor_id, reply_markup=reply_markup
            )
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            await self._bot.edit_message_text(
                text, chat_id=self._chat_id, message_id=self._anchor_id, reply_markup=reply_markup
            )
        except TelegramBadRequest as exc:
            # Re-rendering an unchanged screen (the same validation error twice in a row)
            # is a no-op, not a failure. Anything else means the anchor is unusable —
            # deleted by the user, too old to edit — and a fresh message has to take over.
            if _NOT_MODIFIED in str(exc):
                return True
            log.debug("screen edit failed, sending a new message instead: %s", exc)
            self._anchor_id = None
            return False
        return True

    async def _remember(self) -> None:
        await self._state.update_data({SCREEN_KEY: self._anchor_id})


async def screen_for(event: Message | CallbackQuery, state: FSMContext) -> Screen:
    """Builds the Screen for whatever triggered this handler.

    Reads the anchor out of FSM state *now*, before any handler gets a chance to
    `state.clear()`: the wizards clear state on entry, and an anchor that died
    with the clear would mean every flow boundary (menu → /add, /search → /add)
    starts a new message — exactly the wall of messages this replaces. The id is
    carried in memory across the clear and re-persisted on the next render.
    """
    data = await state.get_data()
    stored = data.get(SCREEN_KEY)
    anchor_id = stored if isinstance(stored, int) else None

    if isinstance(event, CallbackQuery):
        assert event.bot is not None
        message = event.message
        if isinstance(message, Message):
            return Screen(event.bot, message.chat.id, state, anchor_id=message.message_id)
        # An "inaccessible" message (too old for Telegram to hand back) still knows its
        # chat; the anchor then falls back to whatever state remembers.
        chat_id = message.chat.id if message is not None else event.from_user.id
        return Screen(event.bot, chat_id, state, anchor_id=anchor_id)

    assert event.bot is not None
    if anchor_id is not None and event.message_id - anchor_id > _STALE_AFTER_MESSAGES:
        anchor_id = None
    return Screen(event.bot, event.chat.id, state, anchor_id=anchor_id, input_message_id=event.message_id)
