import base64
import contextlib
import json
import os
import re
import subprocess
import tempfile
from asyncio import sleep
from io import BytesIO
from typing import Any, Dict, List, Optional

import httpx
from pyrogram import Client
from pyrogram.enums import MessageEntityType
from pyrogram.errors import Flood
from pyrogram.raw.functions.messages import GetStickerSet
from pyrogram.raw.functions.stickers import AddStickerToSet, CreateStickerSet
from pyrogram.raw.types import (
    InputDocument,
    InputStickerSetItem,
    InputStickerSetShortName,
)
from pyrogram.raw.types.messages import StickerSet
from pyrogram.types import Message

from pagermaid.enums import AsyncClient
from pagermaid.listener import listener
from pagermaid.services import bot
from pagermaid.utils import alias_command

# ============ 配置区 ============

TIMEOUT = 60
PYTHON_PATH = "python3"
QUOTE_API_URL = "https://quote-api-enhanced.zhetengsha.eu.org/generate.webp"
QUOTE_API_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "TeleBox/0.2.1",
}

CONFIG_DIR = os.path.join("data", "yvlu")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


# ============ 工具函数 ============


def _hash_code(s: str) -> int:
    h = 0
    for ch in s:
        h = ((h << 5) - h + ord(ch)) | 0
    return h


def _ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _load_config() -> Dict[str, Any]:
    _ensure_config_dir()
    if not os.path.exists(CONFIG_PATH):
        default = {
            "stickerSetShortName": "",
            "_comment": "如果贴纸包不存在，将自动创建。shortName 只能包含字母、数字和下划线",
        }
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        return default
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"stickerSetShortName": ""}


def _save_config(cfg: Dict[str, Any]):
    _ensure_config_dir()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def is_webm_format(data: bytes) -> bool:
    return bool(data and len(data) >= 4 and data[:4] == b"\x1a\x45\xdf\xa3")


def is_tgs_format(data: bytes) -> bool:
    return bool(data and len(data) >= 2 and data[:2] == b"\x1f\x8b")


def is_animated_webp(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return False
    return b"ANIM" in data[12:]


def is_mp4_format(data: bytes) -> bool:
    if not data or len(data) < 12:
        return False
    return data[4:8] == b"ftyp"


def get_webp_dimensions(data: bytes) -> Dict[str, int]:
    if is_webm_format(data):
        return {"width": 512, "height": 512}
    try:
        if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError("not webp")
        chunk = data[12:16]
        if chunk == b"VP8 ":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L":
            val = int.from_bytes(data[21:25], "little")
            width = (val & 0x3FFF) + 1
            height = ((val >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8X":
            width = (int.from_bytes(data[24:27], "little")) + 1
            height = (int.from_bytes(data[27:30], "little")) + 1
        else:
            raise ValueError("unknown webp chunk")
        return {"width": width, "height": height}
    except Exception:
        return {"width": 512, "height": 768}


async def check_tgs_dependencies() -> Dict[str, Any]:
    try:
        subprocess.run(
            [PYTHON_PATH, "-c", "from rlottie_python import LottieAnimation"],
            check=True,
            capture_output=True,
        )
    except Exception:
        return {
            "ok": False,
            "message": "缺少 rlottie-python 依赖，请运行: pip3 install rlottie-python Pillow",
        }
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except Exception:
        return {"ok": False, "message": "缺少 ffmpeg，请安装: apt-get install -y ffmpeg"}
    return {"ok": True, "message": ""}


async def convert_tgs_to_webm(tgs_buffer: bytes) -> bytes:
    unique_id = f"{os.getpid()}_{os.urandom(4).hex()}"
    tmp_dir = tempfile.gettempdir()
    tgs_path = os.path.join(tmp_dir, f"sticker_{unique_id}.tgs")
    gif_path = os.path.join(tmp_dir, f"sticker_{unique_id}.gif")
    webm_path = os.path.join(tmp_dir, f"sticker_{unique_id}.webm")
    try:
        with open(tgs_path, "wb") as f:
            f.write(tgs_buffer)
        script = (
            "import sys\n"
            "from rlottie_python import LottieAnimation\n"
            "anim = LottieAnimation.from_tgs(sys.argv[1])\n"
            "anim.save_animation(sys.argv[2])\n"
        )
        subprocess.run(
            [PYTHON_PATH, "-c", script, tgs_path, gif_path],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                gif_path,
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-b:v",
                "400k",
                "-auto-alt-ref",
                "0",
                "-an",
                "-y",
                webm_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(webm_path, "rb") as f:
            return f.read()
    finally:
        for p in (tgs_path, gif_path, webm_path):
            with contextlib.suppress(Exception):
                os.unlink(p)


async def convert_mp4_to_webm(mp4_buffer: bytes) -> bytes:
    unique_id = f"{os.getpid()}_{os.urandom(4).hex()}"
    tmp_dir = tempfile.gettempdir()
    mp4_path = os.path.join(tmp_dir, f"video_{unique_id}.mp4")
    webm_path = os.path.join(tmp_dir, f"video_{unique_id}.webm")
    try:
        with open(mp4_path, "wb") as f:
            f.write(mp4_buffer)
        subprocess.run(
            [
                "ffmpeg",
                "-i",
                mp4_path,
                "-c:v",
                "libvpx-vp9",
                "-pix_fmt",
                "yuva420p",
                "-b:v",
                "400k",
                "-auto-alt-ref",
                "0",
                "-an",
                "-y",
                webm_path,
            ],
            check=True,
            capture_output=True,
        )
        with open(webm_path, "rb") as f:
            return f.read()
    finally:
        for p in (mp4_path, webm_path):
            with contextlib.suppress(Exception):
                os.unlink(p)


async def generate_quote(quote_data: Dict[str, Any], request: AsyncClient) -> bytes:
    response = await request.post(
        QUOTE_API_URL,
        headers=QUOTE_API_HEADERS,
        json=quote_data,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.content


# ============ 实体转换 ============


def convert_entities(entities: Optional[List[Any]]) -> List[Dict[str, Any]]:
    if not entities:
        return []
    result = []
    for e in entities:
        if e is None:
            continue
        base = {"offset": e.offset, "length": e.length}
        t = e.type
        if t == MessageEntityType.BOLD:
            result.append({**base, "type": "bold"})
        elif t == MessageEntityType.ITALIC:
            result.append({**base, "type": "italic"})
        elif t == MessageEntityType.UNDERLINE:
            result.append({**base, "type": "underline"})
        elif t == MessageEntityType.STRIKETHROUGH:
            result.append({**base, "type": "strikethrough"})
        elif t == MessageEntityType.CODE:
            result.append({**base, "type": "code"})
        elif t == MessageEntityType.PRE:
            result.append({**base, "type": "pre"})
        elif t == MessageEntityType.CUSTOM_EMOJI:
            result.append(
                {**base, "type": "custom_emoji", "custom_emoji_id": str(e.custom_emoji_id)}
            )
        elif t == MessageEntityType.URL:
            result.append({**base, "type": "url"})
        elif t == MessageEntityType.TEXT_LINK:
            result.append({**base, "type": "text_link", "url": e.url or ""})
        elif t == MessageEntityType.MENTION:
            result.append({**base, "type": "mention"})
        elif t == MessageEntityType.TEXT_MENTION:
            result.append({**base, "type": "text_mention", "user": {"id": e.user.id}})
        elif t == MessageEntityType.HASHTAG:
            result.append({**base, "type": "hashtag"})
        elif t == MessageEntityType.CASHTAG:
            result.append({**base, "type": "cashtag"})
        elif t == MessageEntityType.BOT_COMMAND:
            result.append({**base, "type": "bot_command"})
        elif t == MessageEntityType.EMAIL:
            result.append({**base, "type": "email"})
        elif t == MessageEntityType.PHONE_NUMBER:
            result.append({**base, "type": "phone_number"})
        elif t == MessageEntityType.SPOILER:
            result.append({**base, "type": "spoiler"})
        else:
            result.append(base)
    return result


# ============ 发送者解析 ============


async def resolve_forward_sender(fwd: Any, client: Client) -> Optional[Any]:
    if not fwd:
        return None
    display_name = (
        getattr(fwd, "from_name", None)
        or getattr(fwd, "saved_from_name", None)
        or getattr(fwd, "post_author", None)
        or ""
    )
    fallback_name = display_name or "未知来源"

    peer_candidates = [
        getattr(fwd, "from_user", None),
        getattr(fwd, "from_chat", None),
        getattr(fwd, "saved_from_user", None),
        getattr(fwd, "saved_from_chat", None),
    ]
    for peer in peer_candidates:
        if peer is not None:
            return peer

    class _FallbackSender:
        pass

    fs = _FallbackSender()
    fs.id = _hash_code(fallback_name)
    fs.first_name = fallback_name
    fs.last_name = ""
    fs.username = getattr(fwd, "post_author", None) or ""
    fs.title = fallback_name
    fs.name = fallback_name
    return fs


async def get_message_sender(
    message: Message, client: Client, fake_sender: Optional[Any] = None
) -> Optional[Any]:
    if fake_sender:
        return fake_sender

    sender = message.from_user or message.sender_chat
    if not sender and message.forward_from:
        sender = message.forward_from
    if not sender and message.forward_sender_name:
        sender = await resolve_forward_sender(message.forward_date, client)

    if not sender:
        try:
            sender = await client.get_chat(message.chat.id)
        except Exception:
            pass

    if not sender:
        fallback_name = "未知来源"

        class _FallbackSender:
            pass

        fs = _FallbackSender()
        fs.id = _hash_code(fallback_name)
        fs.first_name = fallback_name
        fs.last_name = ""
        fs.username = ""
        fs.title = fallback_name
        fs.name = fallback_name
        return fs

    return sender


async def download_profile_photo_base64(client: Client, sender: Any) -> Optional[str]:
    try:
        photo = getattr(sender, "photo", None)
        if not photo:
            return None
        file_id = photo.small_file_id
        if not file_id:
            return None
        tmp_path = os.path.join(tempfile.gettempdir(), f"yvlu_avatar_{os.urandom(4).hex()}.jpg")
        try:
            await client.download_media(file_id, file_name=tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            if data:
                return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
        finally:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)
    except Exception as e:
        print(f"[yvlu] 下载头像失败: {e}")
    return None


# ============ 媒体处理 ============


async def process_media(message: Message, client: Client) -> Optional[Dict[str, str]]:
    try:
        sticker = message.sticker
        photo = message.photo
        document = message.document
        animation = message.animation
        video = message.video

        if not any([sticker, photo, document, animation, video]):
            return None

        is_sticker = bool(sticker)
        mime_type = ""
        is_animated = False
        file_id = None

        if sticker:
            file_id = sticker.file_id
            mime_type = sticker.mime_type or ""
            is_animated = sticker.is_animated or sticker.is_video
        elif animation:
            file_id = animation.file_id
            mime_type = animation.mime_type or "video/mp4"
            is_animated = True
        elif video:
            file_id = video.file_id
            mime_type = video.mime_type or "video/mp4"
            is_animated = True
        elif document:
            file_id = document.file_id
            mime_type = document.mime_type or ""
        elif photo:
            file_id = photo.file_id
            mime_type = "image/jpeg"

        tmp_path = os.path.join(tempfile.gettempdir(), f"yvlu_media_{os.urandom(4).hex()}.bin")
        try:
            await client.download_media(file_id, file_name=tmp_path)
            with open(tmp_path, "rb") as f:
                data = f.read()
            if not data:
                return None
        finally:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)

        final_data = data
        final_mime = mime_type

        is_tgs = (
            is_sticker and mime_type == "application/x-tgsticker"
        ) or is_tgs_format(data)
        is_gif_or_mp4 = mime_type in ("video/mp4", "image/gif")
        is_animated_content = is_animated or is_gif_or_mp4 or is_tgs

        if is_tgs:
            dep = await check_tgs_dependencies()
            if dep["ok"]:
                try:
                    final_data = await convert_tgs_to_webm(data)
                    final_mime = "video/webm"
                except Exception as e:
                    print(f"[yvlu] TGS 转换失败: {e}")
            else:
                print(f"[yvlu] {dep['message']}")
        elif is_gif_or_mp4 or is_mp4_format(data):
            try:
                final_data = await convert_mp4_to_webm(data)
                final_mime = "video/webm"
            except Exception as e:
                print(f"[yvlu] MP4 转换失败: {e}")

        if not final_mime:
            final_mime = "image/webp" if is_sticker else "image/jpeg"

        return {
            "url": f"data:{final_mime};base64,{base64.b64encode(final_data).decode()}"
        }
    except Exception as e:
        print(f"[yvlu] 下载媒体失败: {e}")
    return None


# ============ 贴纸包保存 ============


async def get_pack(name: str) -> Optional[StickerSet]:
    try:
        return await bot.invoke(
            GetStickerSet(stickerset=InputStickerSetShortName(short_name=name), hash=0)
        )
    except Exception:
        return None


async def create_sticker_set(name: str, title: str, stickers: List[InputStickerSetItem]):
    me = await bot.get_me()
    await bot.invoke(
        CreateStickerSet(
            user_id=await bot.resolve_peer(me.id),
            title=title,
            short_name=name,
            stickers=stickers,
            software="pagermaid-pyro",
        )
    )


async def _prepare_sticker_document(reply: Message) -> Optional[InputDocument]:
    from pyrogram.file_id import FileId

    if reply.sticker:
        decoded = FileId.decode(reply.sticker.file_id)
    else:
        tmp_path = os.path.join(tempfile.gettempdir(), f"yvlu_sticker_{os.urandom(4).hex()}.webp")
        try:
            await bot.download_media(reply, file_name=tmp_path)
            with open(tmp_path, "rb") as f:
                sent = await bot.send_document("me", f, file_name="sticker.webp")
            decoded = FileId.decode(sent.document.file_id)
            await sent.delete()
        finally:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)
    return InputDocument(
        id=decoded.media_id,
        access_hash=decoded.access_hash,
        file_reference=decoded.file_reference,
    )


async def handle_save_sticker_to_set(message: Message):
    cfg = _load_config()
    name = cfg.get("stickerSetShortName", "")
    if not name:
        return await message.edit(
            f"未配置贴纸包\n请使用 <code>,{alias_command('yvlu')} config sticker 贴纸包名</code> 设置\n"
            f"或编辑配置文件: {CONFIG_PATH}"
        )

    reply = message.reply_to_message
    if not reply:
        return await message.edit("请回复一张贴纸或图片")

    sticker_msg = reply.sticker
    photo_msg = reply.photo
    document_msg = reply.document

    if not sticker_msg and not photo_msg and not document_msg:
        return await message.edit("回复的消息不包含贴纸或图片")

    await message.edit("正在保存到贴纸包...")

    try:
        existing = await get_pack(name)
        if existing is None:
            first_sticker = await _prepare_sticker_document(reply)
            if first_sticker is None:
                return await message.edit("无法准备贴纸数据")
            await create_sticker_set(
                name,
                name,
                [InputStickerSetItem(document=first_sticker, emoji="sticker")],
            )
            return await message.edit(
                f"已创建贴纸包并添加第一个贴纸\n贴纸包: t.me/addstickers/{name}"
            )

        if sticker_msg:
            from pyrogram.file_id import FileId

            decoded = FileId.decode(sticker_msg.file_id)
            doc = InputDocument(
                id=decoded.media_id,
                access_hash=decoded.access_hash,
                file_reference=decoded.file_reference,
            )
        elif photo_msg or document_msg:
            doc = await _prepare_sticker_document(reply)
        else:
            return await message.edit("不支持的媒体类型")

        await bot.invoke(
            AddStickerToSet(
                stickerset=InputStickerSetShortName(short_name=name),
                sticker=InputStickerSetItem(document=doc, emoji="sticker"),
            )
        )
        await message.edit(f"已成功添加到贴纸包\n贴纸包: t.me/addstickers/{name}")
    except Exception as e:
        print(f"[yvlu] 保存贴纸失败 {e}")
        await message.edit(f"保存贴纸失败 {e}")


# ============ 配置管理 ============


async def handle_config_command(message: Message, args: List[str]):
    cfg = _load_config()
    if not args:
        sticker_name = cfg.get("stickerSetShortName", "") or ""
        if sticker_name:
            link_line = f"<b>贴纸包链接:</b> t.me/addstickers/{sticker_name}\n"
        else:
            link_line = ""
        info = (
            f"<b>当前配置:</b>\n\n"
            f"<b>贴纸包名称:</b> <code>{sticker_name or '(未设置)'}</code>\n"
            f"{link_line}\n"
            f"<b>配置文件路径:</b>\n<code>{CONFIG_PATH}</code>\n\n"
            f"<b>可用配置命令:</b>\n"
            f"<code>,{alias_command('yvlu')} config sticker 贴纸包名称</code> - 设置贴纸包名称"
        )
        return await message.edit(info, parse_mode="html")

    sub = args[0].lower()
    if sub in ("sticker", "stickerset", "set"):
        new_name = "_".join(args[1:])
        if not new_name:
            return await message.edit(
                f"请提供贴纸包名称\n用法: <code>,{alias_command('yvlu')} config sticker 贴纸包名称</code>",
                parse_mode="html",
            )
        if not re.match(r"^[a-zA-Z0-9_]+$", new_name):
            return await message.edit(
                "贴纸包名称只能包含字母、数字和下划线", parse_mode="html"
            )
        if not 1 <= len(new_name) <= 64:
            return await message.edit(
                "贴纸包名称长度应在 1-64 个字符之间", parse_mode="html"
            )
        cfg["stickerSetShortName"] = new_name
        _save_config(cfg)
        return await message.edit(
            f"贴纸包名称已设置为 <code>{new_name}</code>\n贴纸包链接: t.me/addstickers/{new_name}",
            parse_mode="html",
        )

    await message.edit(
        f"未知的配置项 <code>{sub}</code>\n\n"
        f"可用配置命令:\n"
        f"<code>,{alias_command('yvlu')} config sticker 贴纸包名称</code> - 设置贴纸包名称",
        parse_mode="html",
    )


# ============ 主命令 ============


HELP_TEXT = (
    "<b>YVLU 语录贴纸生成器</b>\n\n"
    "- 不包含回复\n"
    "使用 <code>,'yvlu' [消息数]</code> 回复一条消息（支持选择部分引用回复）⚠️ 不得超过 5 条\n\n"
    "- 包含回复\n"
    "使用 <code>,'yvlu' r [消息数]</code> 回复一条消息（支持选择部分引用回复）⚠️ 不得超过 5 条\n\n"
    "- 保存贴纸/图片到贴纸包\n"
    "使用 <code>,'yvlu' s</code> 回复一张贴纸或图片，将其保存到配置的贴纸包中\n\n"
    "- 配置管理\n"
    "使用 <code>,'yvlu' config</code> 查看当前配置\n"
    "使用 <code>,'yvlu' config sticker 贴纸包名称</code> 设置贴纸包名称\n\n"
    "- 伪造消息\n"
    "<code>,'yvlu' f 伪造消息</code> 回复一条消息（支持富文本格式）⚠️ 慎用\n"
    "  - 包含回复\n"
    "  <code>,'yvlu' fr 伪造消息</code>\n\n"
    "- 伪造发送者\n"
    "<code>,'yvlu' u 用户ID/用户名 [消息数]</code> 回复一条消息 ⚠️ 慎用\n"
    "  - 包含回复\n"
    "  <code>,'yvlu' ur 用户ID/用户名 [消息数]</code>"
)


@listener(
    command="yvlu",
    description="将回复的消息或者输入的字符串转换成语录",
    parameters="[r|s|config|f|fr|u|ur] [参数]",
)
async def yv_lu(bot: Client, message: Message, request: AsyncClient):
    params = message.parameter or []
    text = message.text or message.caption or ""

    count = 1
    include_reply = False
    save_to_set = False
    fake_msg_text: Optional[str] = None
    fake_msg_entities: Optional[List[Any]] = None
    fake_sender: Optional[Any] = None
    valid = False

    if params and params[0].lower() == "config":
        return await handle_config_command(message, params[1:])

    if not params:
        count = 1
        valid = True
    elif params[0].isdigit():
        count = int(params[0])
        valid = True
    elif params[0] == "r":
        include_reply = True
        count = int(params[1]) if len(params) > 1 and params[1].isdigit() else 1
        valid = True
    elif params[0] == "s":
        save_to_set = True
        valid = True
    elif params[0] in ("u", "ur") and len(params) > 1:
        include_reply = params[0] == "ur"
        if len(params) > 2 and params[2].isdigit():
            count = int(params[2])
        try:
            fake_sender = await bot.get_users(params[1])
        except Exception:
            try:
                fake_sender = await bot.get_chat(params[1])
            except Exception:
                return await message.edit(
                    f"无法获取 {params[1]} 的信息，请检查用户ID/用户名是否正确"
                )
        valid = True
    elif params[0] in ("f", "fr") and len(params) > 1:
        include_reply = params[0] == "fr"
        prefix_match = re.match(r"^(\S+)\s+f[r]?\s+", text)
        if not prefix_match:
            return await message.edit(
                f"格式错误，请使用: <code>,{alias_command('yvlu')} {params[0]} 内容</code>",
                parse_mode="html",
            )
        cut_len = len(prefix_match.group(0))
        fake_msg_text = text[cut_len:]
        raw_entities = list(message.entities or [])
        new_entities = []
        for e in raw_entities:
            start = e.offset
            end = e.offset + e.length
            if end <= cut_len:
                continue
            new_e = type(e)(
                type=e.type,
                offset=max(start - cut_len, 0),
                length=end - max(start, cut_len),
            )
            if hasattr(e, "url"):
                new_e.url = e.url
            if hasattr(e, "user"):
                new_e.user = e.user
            if hasattr(e, "language"):
                new_e.language = e.language
            if hasattr(e, "custom_emoji_id"):
                new_e.custom_emoji_id = e.custom_emoji_id
            new_entities.append(new_e)
        fake_msg_entities = new_entities
        valid = True

    if save_to_set:
        return await handle_save_sticker_to_set(message)

    if not valid:
        return await message.edit(HELP_TEXT, parse_mode="html")

    replied = message.reply_to_message
    if not replied:
        return await message.edit("请回复一条消息")

    if count > 5:
        return await message.edit("太多了 哒咩")

    await message.edit("正在生成语录贴纸...")

    try:
        messages = []
        current = replied
        for _ in range(count):
            messages.append(current)
            if len(messages) >= count:
                break
            try:
                nxt = await bot.get_messages(current.chat.id, current.id + 1)
                if not nxt or nxt.empty:
                    break
                current = nxt
            except Exception:
                break

        if not messages:
            return await message.edit("未找到消息")

        items = []
        previous_user_identifier: Optional[str] = None

        for i, msg in enumerate(messages):
            sender = await get_message_sender(msg, bot, fake_sender)
            if not sender:
                return await message.edit("无法获取消息发送者信息")

            user_id = str(getattr(sender, "id", ""))
            first_name = getattr(sender, "first_name", "") or getattr(sender, "title", "") or ""
            last_name = getattr(sender, "last_name", "") or ""
            username = getattr(sender, "username", "") or ""
            name = (
                getattr(sender, "title", None)
                or f"{first_name} {last_name}".strip()
                or username
            )
            emoji_status = None

            current_user_identifier = user_id or str(_hash_code(name or f"user_{i}"))
            should_show_avatar = current_user_identifier != previous_user_identifier
            previous_user_identifier = current_user_identifier

            photo = None
            if should_show_avatar:
                photo_url = await download_profile_photo_base64(bot, sender)
                if photo_url:
                    photo = {"url": photo_url}

            msg_text = msg.text or msg.caption or ""
            msg_entities = list(msg.entities or msg.caption_entities or [])
            if i == 0:
                if fake_msg_text is not None:
                    msg_text = fake_msg_text
                    msg_entities = fake_msg_entities or []
                if message.quote and getattr(message.quote, "text", None):
                    msg_text = message.quote.text
                    msg_entities = list(getattr(message.quote, "entities", []) or [])

            entities = convert_entities(msg_entities)

            reply_block = None
            if include_reply:
                try:
                    reply_msg = msg.reply_to_message
                    if reply_msg:
                        reply_sender = reply_msg.from_user or reply_msg.sender_chat
                        reply_name = "unknown"
                        reply_chat_id = None
                        if reply_sender:
                            reply_chat_id = reply_sender.id
                            r_first = (
                                getattr(reply_sender, "first_name", "") or ""
                            ) or getattr(reply_sender, "title", "") or ""
                            r_last = getattr(reply_sender, "last_name", "") or ""
                            r_user = getattr(reply_sender, "username", "") or ""
                            reply_name = (
                                f"{r_first} {r_last}".strip() or r_user or "unknown"
                            )
                        reply_text = reply_msg.text or reply_msg.caption or ""
                        reply_entities = convert_entities(
                            reply_msg.entities or reply_msg.caption_entities or []
                        )
                        if reply_text:
                            reply_block = {
                                "name": reply_name,
                                "text": reply_text,
                                "entities": reply_entities,
                            }
                            if reply_chat_id:
                                reply_block["chatId"] = reply_chat_id
                except Exception as e:
                    print(f"[yvlu] 处理回复引用失败: {e}")

            media = await process_media(msg, bot)

            item = {
                "from": {
                    "id": int(user_id) if user_id.isdigit() else _hash_code(name),
                    "name": name if should_show_avatar else "",
                    "first_name": first_name if should_show_avatar else None,
                    "last_name": last_name if should_show_avatar else None,
                    "username": username if (photo and should_show_avatar) else None,
                    "photo": photo,
                    "emoji_status": emoji_status if should_show_avatar else None,
                },
                "text": msg_text,
                "entities": entities,
                "avatar": should_show_avatar,
            }
            if media:
                item["media"] = media
            if reply_block:
                item["replyMessage"] = reply_block
            items.append(item)

        quote_data = {
            "type": "quote",
            "format": "webp",
            "backgroundColor": "#1b1429",
            "width": 512,
            "height": 768,
            "scale": 2,
            "emojiBrand": "apple",
            "messages": items,
        }

        image_buffer = await generate_quote(quote_data, request)
        if not image_buffer:
            return await message.edit("生成的图片数据为空")

        dimensions = get_webp_dimensions(image_buffer)
        is_webm = is_webm_format(image_buffer)

        if is_webm:
            webm_path = os.path.join(
                tempfile.gettempdir(), f"yvlu_{os.urandom(4).hex()}.webm"
            )
            try:
                with open(webm_path, "wb") as f:
                    f.write(image_buffer)
                await bot.send_sticker(
                    message.chat.id,
                    webm_path,
                    reply_to_message_id=replied.id,
                    message_thread_id=message.message_thread_id,
                )
            finally:
                with contextlib.suppress(Exception):
                    os.unlink(webm_path)
        else:
            buf = BytesIO(image_buffer)
            buf.name = "sticker.webp"
            await bot.send_sticker(
                message.chat.id,
                buf,
                reply_to_message_id=replied.id,
                message_thread_id=message.message_thread_id,
            )

        await message.safe_delete()

    except httpx.HTTPError as e:
        print(f"[yvlu] quote-api 请求失败: {e}")
        await message.edit(f"语录生成失败: quote-api 请求失败 ({e})")
    except Flood as e:
        await sleep(e.value + 1)
    except Exception as e:
        print(f"[yvlu] 语录生成失败: {e}")
        await message.edit(f"语录生成失败: {e}")
