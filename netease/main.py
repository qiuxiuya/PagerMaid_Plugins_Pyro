""" PagerMaid Netease Plugin , Thanks to @Music163bot """

import re
from typing import Optional

from pyrogram.errors import YouBlockedUser
from pyrogram import filters

from pagermaid.listener import listener
from pagermaid.enums import Message
from pagermaid.services import bot
from pagermaid.utils import alias_command


Netease_Help_Msg = f"""
网易云搜/点歌。
i.e.
`,{alias_command('netease')} 失眠飞行 兔籽鲸 / 雨客Yoker`  # 通过歌曲名称+歌手（可选）点歌
`,{alias_command('netease')} https://music.163.com/#/song?id=1430702717`  # 通过歌曲链接点歌
`,{alias_command('netease')} 1430702717`  # 通过歌曲 ID 点歌
"""


def _clean_caption(caption: Optional[str]) -> Optional[str]:
    """移除音频说明中最后一行的署名（如 "via @xxx"）。"""
    if not caption:
        return caption
    lines = caption.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.search(r"via\s*@", lines[-1], re.IGNORECASE):
        lines.pop()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


async def _copy_audio(answer: Message, message: Message) -> None:
    """转发音频到目标会话，并去掉说明末尾的 via 署名行。"""
    await answer.copy(
        message.chat.id,
        caption=_clean_caption(answer.caption),
        reply_to_message_id=message.reply_to_message_id,
        message_thread_id=message.message_thread_id,
    )
    await message.safe_delete()


async def netease_start() -> None:
    try:
        await bot.send_message("Music163bot", "/start")
    except YouBlockedUser:
        await bot.unblock_user("Music163bot")


async def netease_search(keyword: str, message: Message):
    async with bot.conversation("Music163bot") as conv:
        await conv.send_message(f"/search {keyword}")
        await conv.mark_as_read()
        answer: Message = await conv.get_response(filters=~filters.regex("搜索中..."))
        await conv.mark_as_read()
        if not answer.reply_markup:
            return await message.edit(answer.text.html)
        await bot.request_callback_answer(
            answer.chat.id,
            answer.id,
            callback_data=answer.reply_markup.inline_keyboard[0][0].callback_data,
        )
        await conv.mark_as_read()
        answer: Message = await conv.get_response(filters=filters.audio)
        await conv.mark_as_read()
        await _copy_audio(answer, message)


async def netease_url(url: str, message: Message):
    async with bot.conversation("Music163bot") as conv:
        await conv.send_message(url)
        await conv.mark_as_read()
        answer: Message = await conv.get_response(filters=filters.audio)
        await conv.mark_as_read()
        await _copy_audio(answer, message)


async def netease_id(music_id: str, message: Message):
    async with bot.conversation("Music163bot") as conv:
        await conv.send_message(f"/music {music_id}")
        await conv.mark_as_read()
        answer: Message = await conv.get_response(filters=filters.audio)
        await conv.mark_as_read()
        await _copy_audio(answer, message)


@listener(
    command="netease",
    description="Netease Music",
    parameters="[query]",
)
async def netease_music(message: Message):
    if not message.arguments:
        return await message.edit(Netease_Help_Msg)
    await netease_start()
    url_match = re.search(r"https?://\S+", message.arguments, re.IGNORECASE)
    if url_match:
        return await netease_url(url_match.group(), message)
    if message.arguments.isdigit():
        return await netease_id(message.arguments, message)
    return await netease_search(message.arguments, message)
