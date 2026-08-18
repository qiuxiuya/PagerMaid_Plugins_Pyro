""" PagerMaid WhatAnime Plugin , Thanks to @WhatAnimeBetaBot """

from io import BytesIO

from pyrogram import filters
from pyrogram.types import InputMediaPhoto

from pagermaid.listener import listener
from pagermaid.enums import Message
from pagermaid.services import bot
from pagermaid.utils import alias_command

WHATANIME_BOT = "WhatAnimeBetaBot"


WhatAnime_Help_Msg = f"""
动漫出处识别。
回复一条包含图片的消息,然后发送 `,{alias_command('anime')}`
"""


@listener(
    command="anime",
    description="WhatAnime: 识别动漫出处",
    parameters="[回复图片消息后使用]",
)
async def whatanime(message: Message):
    if not message.reply_to_message:
        return await message.edit(WhatAnime_Help_Msg)

    reply: Message = message.reply_to_message
    if not reply.photo:
        return await message.edit("请回复一条包含图片的消息。")

    async with bot.conversation(WHATANIME_BOT) as conv:
        await bot.unblock_user(WHATANIME_BOT)
        await conv.send_message("/start")
        await conv.mark_as_read()

        photos = [reply]
        if reply.media_group_id:
            try:
                photos = await bot.get_media_group(
                    message.chat.id, reply.id
                )
            except ValueError:
                photos = [reply]

        downloaded = []
        for photo_msg in photos:
            photo = await bot.download_media(photo_msg, in_memory=True)
            if isinstance(photo, BytesIO) and not photo.name:
                photo.name = "photo.jpg"
            downloaded.append(photo)

        if len(downloaded) > 1:
            await bot.send_media_group(
                WHATANIME_BOT,
                [InputMediaPhoto(photo) for photo in downloaded],
            )
        else:
            await bot.send_photo(WHATANIME_BOT, downloaded[0])

        answer: Message = await conv.get_response(
            filters=~filters.outgoing
            & ~filters.regex("您可以发送或转发动漫截图给我")
        )
        await conv.mark_as_read()
        await answer.copy(
            message.chat.id,
            message_thread_id=message.message_thread_id,
        )
        await conv.mark_as_read()
        await message.safe_delete()
