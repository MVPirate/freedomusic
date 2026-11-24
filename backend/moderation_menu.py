"""Reuse the current Telegram message for moderation navigation."""

import asyncio

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions, Message


_panels: dict[int, Message] = {}
_panel_lock = asyncio.Lock()


async def render_panel(message: Message, text: str, *, markup, edit: bool = True) -> None:
    preview = LinkPreviewOptions(is_disabled=True)
    async with _panel_lock:
        chat_id = message.chat.id
        target = message if edit and message.text else _panels.get(chat_id)
        if target is None and edit and message.audio:
            target = message
        if target is not None:
            try:
                if target.audio:
                    await target.edit_caption(caption=text, reply_markup=markup)
                else:
                    await target.edit_text(text, reply_markup=markup, link_preview_options=preview)
            except TelegramBadRequest as exc:
                detail = exc.message.lower()
                if "message is not modified" in detail:
                    _panels[chat_id] = target
                    return
                if "message to edit not found" not in detail and "message can't be edited" not in detail:
                    raise
            else:
                _panels[chat_id] = target
                return
        _panels[chat_id] = await message.answer(text, reply_markup=markup, link_preview_options=preview)
