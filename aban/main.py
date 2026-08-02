"""PagerMaid-Pyro 封禁管理插件"""

import asyncio
import contextlib
import html
import json
import re
from asyncio import sleep
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import (
    BadRequest,
    ChatAdminRequired,
    FloodWait,
    PeerIdInvalid,
    UserAdminInvalid,
    UserNotParticipant,
)
from pyrogram.types import Chat, ChatMember, ChatPermissions, User

from pagermaid.dependence import add_delete_message_job
from pagermaid.listener import listener
from pagermaid.enums import Message
from pagermaid.services import bot

HELP_TEXT = """<b>封禁管理</b>

<code>,kick</code> 踢出
<code>,ban</code> 封禁
<code>,unban</code> 解封
<code>,mute [time]</code> 禁言 (如 60s/5m/1h/1d，不填则永久)
<code>,unmute</code> 解禁言
<code>,sb</code> 批量封禁
<code>,unsb</code> 批量解封
<code>,refresh</code> 刷新

回复消息或@用户名"""

DATA_DIR = Path("data/aban")
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_FILE = DATA_DIR / "cache.json"

RE_TIME = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)


def parse_time_string(time_str: Optional[str]) -> int:
    """解析时间字符串，返回秒数；0 表示永久。"""
    if not time_str:
        return 0
    match = RE_TIME.match(time_str.strip().lower())
    if not match:
        return 0
    num = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 0}
    return num * multipliers.get(unit, 0)


def format_duration(seconds: int) -> str:
    """将秒数格式化为可读字符串。"""
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def load_cache() -> dict:
    """加载本地缓存。"""
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cache(data: dict) -> None:
    """保存本地缓存。"""
    try:
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[aban] 缓存写入失败: {e}")


def cache_get(key: str):
    return load_cache().get(key)


def cache_set(key: str, value) -> None:
    data = load_cache()
    data[key] = value
    save_cache(data)


def cache_clear() -> None:
    save_cache({})


async def smart_edit(
    message: Message,
    text: str,
    delete_after: int = 10,
    parse_mode: str = "html",
) -> Message:
    """编辑消息，并在指定秒后删除。"""
    try:
        parse_mode_enum = ParseMode.HTML if parse_mode == "html" else ParseMode.MARKDOWN
        msg = await message.edit(text, parse_mode=parse_mode_enum, disable_web_page_preview=True)
    except Exception as e:
        print(f"[aban] 编辑消息失败: {e}")
        msg = message
    if delete_after > 0:
        add_delete_message_job(msg, delete_after)
    return msg


async def resolve_target(message: Message) -> tuple[Optional[Union[User, Chat]], Optional[int]]:
    """从参数或回复消息解析目标用户/频道。"""
    text = message.text or message.caption or ""
    params = text.split()[1:] if text else []

    # 从参数解析
    if params:
        target = params[0]
        return await resolve_from_string(target)

    # 从回复消息解析
    reply = message.reply_to_message
    if reply:
        if reply.from_user:
            return reply.from_user, reply.from_user.id
        if reply.sender_chat:
            return reply.sender_chat, reply.sender_chat.id

    return None, None


async def resolve_from_string(target: str) -> tuple[Optional[Union[User, Chat]], Optional[int]]:
    """根据字符串解析用户/频道实体。"""
    try:
        if target.startswith("@"):
            entity = await bot.get_chat(target)
            return entity, entity.id

        if re.match(r"^-?\d+$", target):
            uid = int(target)
            entity = await bot.get_chat(uid)
            return entity, uid
    except Exception as e:
        print(f"[aban] 解析目标失败: {e}")

    return None, None


def format_user(entity: Optional[Union[User, Chat]], user_id: int) -> str:
    """格式化用户/频道显示名称。"""
    if entity is None:
        return str(user_id)

    if isinstance(entity, User):
        name = entity.first_name or ""
        if entity.last_name:
            name += f" {entity.last_name}"
        name = name.strip() or str(user_id)
        if entity.username:
            name += f" (@{entity.username})"
        return name

    if isinstance(entity, Chat):
        title = entity.title or str(user_id)
        if entity.username:
            title += f" (@{entity.username})"
        return f"频道: {title}"

    return str(user_id)


async def is_target_admin(chat: Chat, user_id: int) -> bool:
    """检查目标是否为群组/频道管理员。"""
    with contextlib.suppress(Exception):
        member = await bot.get_chat_member(chat.id, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)
    return False


_bot_id: Optional[int] = None
_bot_id_lock = asyncio.Lock()


async def get_bot_id() -> Optional[int]:
    """获取机器人自身 ID，全局缓存并加锁避免并发 FloodWait。"""
    global _bot_id
    if _bot_id is not None:
        return _bot_id

    async with _bot_id_lock:
        # 双重检查
        if _bot_id is not None:
            return _bot_id
        with contextlib.suppress(Exception):
            if bot.me:
                _bot_id = bot.me.id
                return _bot_id
        try:
            me = await bot.get_me()
            _bot_id = me.id
            return _bot_id
        except FloodWait as e:
            await sleep(e.value)
            me = await bot.get_me()
            _bot_id = me.id
            return _bot_id
        except Exception as e:
            print(f"[aban] 获取机器人 ID 失败: {e}")
    return None


async def can_delete_messages(chat: Chat) -> bool:
    """检查机器人是否有删除消息权限。"""
    bot_id = await get_bot_id()
    if not bot_id:
        return False
    try:
        me = await bot.get_chat_member(chat.id, bot_id)
        if me.status == ChatMemberStatus.OWNER:
            return True
        if me.status == ChatMemberStatus.ADMINISTRATOR and me.privileges:
            return bool(me.privileges.can_delete_messages)
    except Exception:
        pass
    return False


async def can_restrict_members(chat: Chat) -> bool:
    """检查机器人是否有封禁/限制成员权限。"""
    bot_id = await get_bot_id()
    if not bot_id:
        return False
    try:
        me = await bot.get_chat_member(chat.id, bot_id)
        if me.status == ChatMemberStatus.OWNER:
            return True
        if me.status == ChatMemberStatus.ADMINISTRATOR and me.privileges:
            return bool(me.privileges.can_restrict_members)
    except Exception:
        pass
    return False


async def delete_user_history(chat: Chat, user_id: int) -> bool:
    """删除目标在当前会话的历史消息。"""
    if not await can_delete_messages(chat):
        return False
    try:
        await bot.delete_user_history(chat.id, user_id)
        return True
    except Exception as e:
        if "CHANNEL_INVALID" not in str(e) and "CHAT_ADMIN_REQUIRED" not in str(e):
            print(f"[aban] 删除消息失败: {e}")
        return False


async def gather_limited(tasks, limit: int):
    """带并发限制的 gather 实现。"""
    import asyncio

    semaphore = asyncio.Semaphore(limit)

    async def _wrap(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*[_wrap(t) for t in tasks], return_exceptions=True)


async def get_managed_groups() -> list[Chat]:
    """获取机器人具有管理权限的群组/频道列表，带缓存。"""
    cached = cache_get("managed_groups")
    if cached:
        try:
            return [await bot.get_chat(cid) for cid in cached]
        except Exception:
            pass

    dialogs: list[Chat] = []
    try:
        async for dialog in bot.get_dialogs(limit=500):
            chat = dialog.chat
            # 处理所有群组和频道（与 TS 版本行为一致）
            if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                dialogs.append(chat)
    except Exception as e:
        print(f"[aban] 获取对话列表失败: {e}")

    # 并发检查权限，限制并发数避免 FloodWait
    async def _check(chat: Chat) -> Optional[Chat]:
        try:
            if await can_restrict_members(chat):
                return chat
        except Exception as e:
            print(f"[aban] 检查群组权限异常 {chat.id}: {e}")
        return None

    results = await gather_limited([_check(chat) for chat in dialogs], limit=10)
    groups = [chat for chat in results if chat is not None]

    cache_set("managed_groups", [chat.id for chat in groups])
    return groups


async def _ensure_chat(chat_id: int) -> Optional[Chat]:
    """确保获取到带有 access_hash 的完整 chat 对象。"""
    try:
        return await bot.get_chat(chat_id)
    except Exception as e:
        print(f"[aban] 获取聊天对象失败: {e}")
        return None


async def kick_user(chat: Chat, user_id: int) -> bool:
    """踢出用户：先封禁再解封。"""
    chat = await _ensure_chat(chat.id) or chat
    try:
        await bot.ban_chat_member(chat.id, user_id)
        await bot.unban_chat_member(chat.id, user_id)
        return True
    except Exception as e:
        print(f"[aban] 踢出失败: {e}")
        return False


async def ban_user(chat: Chat, user_id: int, until: Optional[datetime] = None) -> bool:
    """封禁用户。"""
    chat = await _ensure_chat(chat.id) or chat
    try:
        await bot.ban_chat_member(chat.id, user_id, until_date=until)
        return True
    except Exception as e:
        print(f"[aban] 封禁失败: {e}")
        return False


async def unban_user(chat: Chat, user_id: int) -> bool:
    """解封用户。"""
    chat = await _ensure_chat(chat.id) or chat
    try:
        await bot.unban_chat_member(chat.id, user_id)
        return True
    except Exception as e:
        print(f"[aban] 解封失败: {e}")
        return False


async def mute_user(chat: Chat, user_id: int, duration: int) -> bool:
    """禁言用户；duration=0 表示永久。"""
    chat = await _ensure_chat(chat.id) or chat
    try:
        until_date = None
        if duration > 0:
            until_date = datetime.utcnow() + timedelta(seconds=duration)
        await bot.restrict_chat_member(chat.id, user_id, ChatPermissions(), until_date=until_date)
        return True
    except Exception as e:
        print(f"[aban] 禁言失败: {e}")
        return False


async def unmute_user(chat: Chat, user_id: int) -> bool:
    """解除禁言：恢复默认权限。"""
    chat = await _ensure_chat(chat.id) or chat
    try:
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_change_info=True,
            can_invite_users=True,
            can_pin_messages=True,
        )
        await bot.restrict_chat_member(chat.id, user_id, permissions)
        return True
    except Exception as e:
        print(f"[aban] 解禁言失败: {e}")
        return False


async def basic_command(message: Message, action: str) -> None:
    """单群基础命令处理：kick/ban/unban/mute/unmute。"""
    chat = message.chat

    entity, uid = await resolve_target(message)
    if not uid:
        await smart_edit(message, "获取用户失败", 10)
        return

    # 检查目标是否为管理员
    text = message.text or message.caption or ""
    params = text.split()[1:] if text else []
    if await is_target_admin(chat, uid) and "true" not in params:
        await smart_edit(
            message,
            "目标是管理员，请在命令后加上 <code>true</code> 确认执行",
            15,
        )
        return

    display = format_user(entity, uid)
    status = await smart_edit(
        message,
        f"{get_action_name(action)}{html.escape(display)}...",
        0,
    )

    success = False
    result_text = ""

    if action == "kick":
        success = await kick_user(chat, uid)
        result_text = f"已踢出 {html.escape(display)}"
    elif action == "ban":
        await delete_user_history(chat, uid)
        success = await ban_user(chat, uid)
        result_text = f"已封禁 {html.escape(display)}"
    elif action == "unban":
        success = await unban_user(chat, uid)
        result_text = f"已解封 {html.escape(display)}"
    elif action == "mute":
        text = message.text or message.caption or ""
        params = text.split()[1:] if text else []
        duration = parse_time_string(params[1] if len(params) > 1 else None)
        success = await mute_user(chat, uid, duration)
        duration_text = "永久" if duration == 0 else format_duration(duration)
        result_text = f"已禁言 {html.escape(display)} {duration_text}"
    elif action == "unmute":
        success = await unmute_user(chat, uid)
        result_text = f"已解禁言 {html.escape(display)}"

    if success:
        await smart_edit(status, result_text, 10)
    else:
        await smart_edit(status, f"{get_action_name(action)}失败", 10)


def get_action_name(action: str) -> str:
    names = {
        "kick": "踢出",
        "ban": "封禁",
        "unban": "解封",
        "mute": "禁言",
        "unmute": "解除禁言",
    }
    return names.get(action, action)


async def super_ban_command(message: Message) -> None:
    """sb 命令：在所有管理的群组/频道中批量封禁用户。"""
    chat = message.chat
    entity, uid = await resolve_target(message)
    if not uid:
        await smart_edit(message, "获取用户失败", 10)
        return

    text = message.text or message.caption or ""
    params = text.split()[1:] if text else []
    if await is_target_admin(chat, uid) and "true" not in params:
        await smart_edit(
            message,
            "目标是管理员，请在命令后加上 <code>true</code> 确认执行",
            15,
        )
        return

    groups = await get_managed_groups()
    if not groups:
        await smart_edit(message, "无管理群组", 10)
        return

    display = format_user(entity, uid)
    status = await smart_edit(
        message,
        f"在{len(groups)}个频道/群组中封禁该用户...",
        0,
    )

    async def background_process():
        try:
            start_time = datetime.now()
            delete_success = await delete_user_history(chat, uid)
            success, failed_groups = await batch_ban_user(groups, uid)
            elapsed = (datetime.now() - start_time).total_seconds()
            result = (
                f"在{success}个频道/群组中封禁该用户 {html.escape(display)}\n"
                f"当前群组消息: {'已清理' if delete_success else '失败'} | {elapsed:.1f}s"
            )
            await smart_edit(status, result, 30)
        except Exception as e:
            print(f"[aban] sb 后台处理失败: {e}")
            await smart_edit(status, f"批量封禁失败: {e}", 30)

    # 后台执行，不阻塞命令响应
    bot.loop.create_task(background_process())


async def super_unban_command(message: Message) -> None:
    """unsb 命令：在所有管理的群组/频道中批量解封用户。"""
    chat = message.chat
    entity, uid = await resolve_target(message)
    if not uid:
        await smart_edit(message, "获取用户失败", 10)
        return

    text = message.text or message.caption or ""
    params = text.split()[1:] if text else []
    if await is_target_admin(chat, uid) and "true" not in params:
        await smart_edit(
            message,
            "目标是管理员，请在命令后加上 <code>true</code> 确认执行",
            15,
        )
        return

    groups = await get_managed_groups()
    if not groups:
        await smart_edit(message, "无管理群组", 10)
        return

    display = format_user(entity, uid)
    status = await smart_edit(
        message,
        f"在{len(groups)}个频道/群组中解封该用户...",
        0,
    )

    async def background_process():
        try:
            start_time = datetime.now()
            success, failed_groups = await batch_unban_user(groups, uid)
            elapsed = (datetime.now() - start_time).total_seconds()
            result = f"在{success}个频道/群组中解封该用户 {html.escape(display)} | {elapsed:.1f}s"
            await smart_edit(status, result, 30)
        except Exception as e:
            print(f"[aban] unsb 后台处理失败: {e}")
            await smart_edit(status, f"批量解封失败: {e}", 30)

    bot.loop.create_task(background_process())


async def batch_ban_user(groups: list[Chat], user_id: int) -> tuple[int, list[str]]:
    """批量封禁用户，返回成功数和失败群组名。"""
    success = 0
    failed_groups: list[str] = []

    async def _ban_one(chat: Chat):
        try:
            await bot.ban_chat_member(chat.id, user_id)
            return True, chat.title or str(chat.id)
        except FloodWait as e:
            await sleep(e.value)
            try:
                await bot.ban_chat_member(chat.id, user_id)
                return True, chat.title or str(chat.id)
            except Exception:
                return False, chat.title or str(chat.id)
        except Exception:
            return False, chat.title or str(chat.id)

    results = await gather_limited([_ban_one(g) for g in groups], limit=20)
    for result in results:
        if isinstance(result, BaseException):
            continue
        ok, title = result
        if ok:
            success += 1
        else:
            failed_groups.append(title)

    return success, failed_groups


async def batch_unban_user(groups: list[Chat], user_id: int) -> tuple[int, list[str]]:
    """批量解封用户，返回成功数和失败群组名。"""
    success = 0
    failed_groups: list[str] = []

    async def _unban_one(chat: Chat):
        try:
            await bot.unban_chat_member(chat.id, user_id)
            return True, chat.title or str(chat.id)
        except FloodWait as e:
            await sleep(e.value)
            try:
                await bot.unban_chat_member(chat.id, user_id)
                return True, chat.title or str(chat.id)
            except Exception:
                return False, chat.title or str(chat.id)
        except Exception:
            return False, chat.title or str(chat.id)

    results = await gather_limited([_unban_one(g) for g in groups], limit=20)
    for result in results:
        if isinstance(result, BaseException):
            continue
        ok, title = result
        if ok:
            success += 1
        else:
            failed_groups.append(title)

    return success, failed_groups


# ==================== 命令注册 ====================


@listener(command="aban", description="封禁管理帮助")
async def aban_help(message: Message):
    await smart_edit(message, HELP_TEXT, 30)


@listener(command="kick", groups_only=True, need_admin=True, description="踢出一位用户")
async def cmd_kick(message: Message):
    await basic_command(message, "kick")


@listener(command="ban", groups_only=True, need_admin=True, description="封禁一位用户")
async def cmd_ban(message: Message):
    await basic_command(message, "ban")


@listener(command="unban", groups_only=True, need_admin=True, description="解封一位用户")
async def cmd_unban(message: Message):
    await basic_command(message, "unmute")


@listener(
    command="mute",
    groups_only=True,
    need_admin=True,
    description="禁言一位用户，支持 60s/5m/1h/1d，不填则永久",
)
async def cmd_mute(message: Message):
    await basic_command(message, "mute")


@listener(command="unmute", groups_only=True, need_admin=True, description="解除禁言")
async def cmd_unmute(message: Message):
    await basic_command(message, "unmute")


@listener(command="sb", groups_only=True, need_admin=True, description="在所有管理群组中批量封禁用户")
async def cmd_sb(message: Message):
    await super_ban_command(message)


@listener(command="unsb", groups_only=True, need_admin=True, description="在所有管理群组中批量解封用户")
async def cmd_unsb(message: Message):
    await super_unban_command(message)


@listener(command="refresh", description="刷新管理群组缓存")
async def cmd_refresh(message: Message):
    status = await smart_edit(message, "刷新中...", 0)
    try:
        cache_clear()
        groups = await get_managed_groups()
        await smart_edit(status, f"已刷新 {len(groups)}个群组", 10)
    except Exception as e:
        await smart_edit(status, f"刷新失败: {e}", 10)
