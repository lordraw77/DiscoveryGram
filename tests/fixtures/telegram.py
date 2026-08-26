"""Test doubles for the Telegram side.

Real `telegram.Update` objects are used rather than mocks, because the handlers
read them through PTB's own properties (`effective_message`, `effective_chat`)
and a mock would let a wrong attribute name pass. Only the *bot* is faked — it
is the one thing that would otherwise make a network call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ContextTypes

ALLOWED_USER_ID = 111
DENIED_USER_ID = 999
CHAT_ID = 111


class FakeBot:
    """Records what would have been sent instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[str] = []
        self.answered_with: list[str] = []
        self.commands: list[Any] = []
        self.fail_with: Exception | None = None

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> None:
        self.answered.append(callback_query_id)
        self.answered_with.append(str(kwargs.get("text", "")))

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)

    async def set_my_commands(self, commands: Any) -> None:
        self.commands = list(commands)

    @property
    def texts(self) -> list[str]:
        return [message["text"] for message in self.sent]

    @property
    def last_text(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""


class FakeContext:
    """The slice of `ContextTypes.DEFAULT_TYPE` the handlers actually use."""

    def __init__(self, bot: FakeBot, bot_data: dict[str, Any]) -> None:
        self.bot = bot
        self.bot_data = bot_data
        self.args: list[str] = []
        self.user_data: dict[Any, Any] = {}
        self.chat_data: dict[Any, Any] = {}
        self.error: BaseException | None = None


def as_context(context: FakeContext) -> ContextTypes.DEFAULT_TYPE:
    """Hand a fake to a handler typed for python-telegram-bot's real context.

    One cast, in one place, instead of a `type: ignore` on every call site —
    which would also suppress the argument errors worth catching.
    """
    return cast(ContextTypes.DEFAULT_TYPE, context)


def make_user(user_id: int = ALLOWED_USER_ID, username: str | None = "tester") -> User:
    return User(id=user_id, first_name="Test", is_bot=False, username=username)


def make_message(bot: FakeBot, *, user_id: int = ALLOWED_USER_ID, text: str = "/help") -> Message:
    message = Message(
        message_id=1,
        date=datetime(2026, 8, 26, tzinfo=UTC),
        chat=Chat(id=CHAT_ID, type=Chat.PRIVATE),
        from_user=make_user(user_id),
        text=text,
    )
    message.set_bot(bot)  # type: ignore[arg-type]
    return message


def make_update(bot: FakeBot, *, user_id: int = ALLOWED_USER_ID, text: str = "/help") -> Update:
    return Update(update_id=1, message=make_message(bot, user_id=user_id, text=text))


def make_callback_update(bot: FakeBot, *, data: str, user_id: int = ALLOWED_USER_ID) -> Update:
    query = CallbackQuery(
        id="cbq-1",
        from_user=make_user(user_id),
        chat_instance="chat-instance",
        data=data,
        message=make_message(bot, user_id=user_id),
    )
    query.set_bot(bot)  # type: ignore[arg-type]
    return Update(update_id=2, callback_query=query)


# --- MarkdownV2 safety ----------------------------------------------------

# Everything MarkdownV2 reserves, per the Bot API formatting reference.
MARKDOWN_V2_RESERVED = set("_*[]()~`>#+-=|{}.!")
# The two we emit deliberately as markup: *bold* and _italic_.
_MARKERS = "*_"


def assert_markdown_v2_safe(text: str) -> None:
    """Fail unless Telegram would accept `text` as MarkdownV2.

    One unescaped reserved character makes the Bot API reject the **whole**
    message with a 400, so this is asserted on every reply the bot composes.

    The scan is a single escape-aware pass rather than a regex sweep: an
    *escaped* backtick does not open a code span, and treating it as one would
    make a perfectly valid body look broken. Inside a code span only the
    backtick is special; outside one, every reserved character must be escaped
    and the markers we emit on purpose must balance.
    """
    counts = dict.fromkeys(_MARKERS, 0)
    backticks = 0
    in_code = False

    index = 0
    while index < len(text):
        char = text[index]

        if char == "\\":
            index += 2  # An escape covers the character after it.
            continue

        if char == "`":
            backticks += 1
            in_code = not in_code
        elif in_code:
            pass  # Only a backtick is special inside a code span.
        elif char in _MARKERS:
            counts[char] += 1
        elif char in MARKDOWN_V2_RESERVED:
            raise AssertionError(f"unescaped {char!r} at {index} in {text!r}")

        index += 1

    if backticks % 2:
        raise AssertionError(f"unbalanced backtick ({backticks} found) in {text!r}")
    for marker, count in counts.items():
        if count % 2:
            raise AssertionError(f"unbalanced {marker!r} ({count} occurrences) in {text!r}")
