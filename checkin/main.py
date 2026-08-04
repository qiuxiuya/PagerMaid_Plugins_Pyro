import contextlib
import datetime
import random
import re

from typing import Optional, List

from pagermaid.dependence import scheduler, sqlite
from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.services import bot
from pagermaid.utils import alias_command


class CheckinConfig:
    KEY = "checkin_config"
    start_time: str
    end_time: str

    def __init__(self, start_time: str = "08:00", end_time: str = "20:00"):
        self.start_time = start_time
        self.end_time = end_time

    def export(self):
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    def save(self):
        sqlite[self.KEY] = self.export()

    @classmethod
    def load(cls):
        data = sqlite.get(cls.KEY, {})
        return cls(
            start_time=data.get("start_time", "08:00"),
            end_time=data.get("end_time", "20:00"),
        )

    @staticmethod
    def parse_time(text: str) -> tuple[int, int]:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", text.strip())
        if not match:
            raise ValueError(f"Invalid time format: {text}")
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Invalid time: {text}")
        return hour, minute

    def set_range(self, start_time: str, end_time: str):
        start_hour, start_minute = self.parse_time(start_time)
        end_hour, end_minute = self.parse_time(end_time)
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        if end_minutes <= start_minutes:
            raise ValueError("End time must be later than start time")
        self.start_time = f"{start_hour:02d}:{start_minute:02d}"
        self.end_time = f"{end_hour:02d}:{end_minute:02d}"

    def random_time(self) -> datetime.time:
        start_hour, start_minute = self.parse_time(self.start_time)
        end_hour, end_minute = self.parse_time(self.end_time)
        start_minutes = start_hour * 60 + start_minute
        end_minutes = end_hour * 60 + end_minute
        random_minutes = random.randint(start_minutes, end_minutes - 1)
        return datetime.time(random_minutes // 60, random_minutes % 60)


class CheckinTask:
    task_id: Optional[int]
    cid: int
    msg: str
    pause: bool

    def __init__(
        self,
        task_id: int,
        cid: int = 0,
        msg: str = "",
        pause: bool = False,
    ):
        self.task_id = task_id
        self.cid = cid
        self.msg = msg
        self.pause = pause

    def export(self):
        return {
            "task_id": self.task_id,
            "cid": self.cid,
            "msg": self.msg,
            "pause": self.pause,
        }

    def get_job(self):
        return scheduler.get_job(f"checkin|{self.cid}|{self.task_id}")

    def remove_job(self):
        if self.get_job():
            scheduler.remove_job(f"checkin|{self.cid}|{self.task_id}")

    def export_str(self, show_all: bool = False):
        text = f"<code>{self.task_id}</code> - "
        if job := self.get_job():
            time: datetime.datetime = job.next_run_time
            text += f"<code>{time.strftime('%Y-%m-%d %H:%M:%S')}</code> - "
        else:
            text += "<code>未运行</code> - "
        if show_all:
            text += f"<code>{self.cid}</code> - "
        text += f"{self.msg}"
        return text


class CheckinTasks:
    tasks: List[CheckinTask]
    config: CheckinConfig

    def __init__(self):
        self.tasks = []
        self.config = CheckinConfig.load()

    def add(self, task: CheckinTask):
        for i in self.tasks:
            if i.task_id == task.task_id:
                return
        self.tasks.append(task)

    def remove(self, task_id: int):
        for task in self.tasks:
            if task.task_id == task_id:
                task.remove_job()
                self.tasks.remove(task)
                return True
        return False

    def get(self, task_id: int) -> Optional[CheckinTask]:
        return next((task for task in self.tasks if task.task_id == task_id), None)

    def get_all(self) -> List[CheckinTask]:
        return self.tasks

    def get_all_ids(self) -> List[int]:
        return [task.task_id for task in self.tasks]

    def print_all_tasks(self, show_all: bool = False, cid: int = 0) -> str:
        return "\n".join(
            task.export_str(show_all)
            for task in self.tasks
            if task.cid == cid or show_all
        )

    def save_to_file(self):
        data = {
            "config": self.config.export(),
            "tasks": [task.export() for task in self.tasks],
        }
        sqlite["checkin_data"] = data

    def load_from_file(self):
        data = sqlite.get("checkin_data", {})
        if config_data := data.get("config"):
            self.config = CheckinConfig(
                start_time=config_data.get("start_time", "08:00"),
                end_time=config_data.get("end_time", "20:00"),
            )
        for i in data.get("tasks", []):
            self.add(CheckinTask(**i))

    def pause_task(self, task_id):
        if task := self.get(task_id):
            task.pause = True
            task.remove_job()
            self.save_to_file()
            return True
        return False

    async def send_message(self, task: CheckinTask):
        with contextlib.suppress(Exception):
            await bot.send_message(task.cid, task.msg)
        # 发送完成后为明天重新随机注册任务
        self.schedule_next_day(task)

    def schedule_next_day(self, task: CheckinTask):
        if task.pause:
            return
        task.remove_job()
        self._register_job_for_date(task, datetime.date.today() + datetime.timedelta(days=1))

    def _register_job_for_date(self, task: CheckinTask, date: datetime.date):
        t = self.config.random_time()
        run_time = datetime.datetime.combine(date, t)
        now = datetime.datetime.now()
        # 如果生成的时间点已过，则顺延到下一天
        if run_time <= now:
            run_time += datetime.timedelta(days=1)
        scheduler.add_job(
            self.send_message,
            "date",
            id=f"checkin|{task.cid}|{task.task_id}",
            name=f"checkin|{task.cid}|{task.task_id}",
            args=[task],
            run_date=run_time,
            replace_existing=True,
        )

    def register_single_task(self, task: CheckinTask):
        if task.pause:
            return
        self._register_job_for_date(task, datetime.date.today())

    def resume_task(self, task_id: int):
        if task := self.get(task_id):
            task.pause = False
            self.register_single_task(task)
            self.save_to_file()
            return True
        return False

    def register_all_tasks(self):
        for task in self.tasks:
            self.register_single_task(task)

    def get_next_task_id(self):
        return max(task.task_id for task in self.tasks) + 1 if self.tasks else 1

    def update_config(self, start_time: str, end_time: str):
        self.config.set_range(start_time, end_time)
        for task in self.tasks:
            if not task.pause:
                task.remove_job()
                self.register_single_task(task)
        self.save_to_file()


async def resolve_cid(text: str, message: Message) -> int | str:
    """Resolve target to ID; fallback to original string like checkin.ts."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        pass
    names = {text, text.lstrip("@")}
    for name in names:
        try:
            peer = await message.client.resolve_peer(name)
            return peer.user_id if hasattr(peer, "user_id") else peer.channel_id if hasattr(peer, "channel_id") else peer.chat_id
        except Exception:
            pass
        try:
            entity = await message.client.get_users(name)
            return entity.id
        except Exception:
            pass
        try:
            entity = await message.client.get_chat(name)
            return entity.id
        except Exception:
            pass
    # Fallback: keep original string, let send_message resolve it at runtime
    return text


checkin_tasks = CheckinTasks()
checkin_tasks.load_from_file()
checkin_tasks.register_all_tasks()

checkin_help_msg = f"""
在全局设定的时间范围内随机发送签到消息。

设置时间范围：
,{alias_command("checkin")} start 8:00 end 20:00

添加任务：
,{alias_command("checkin")} add <用户名/ID> <消息内容>
i.e.
,{alias_command("checkin")} add @username 今天也要记得打卡哦
,{alias_command("checkin")} add 123456789 该签到了


,{alias_command("checkin")} rm 2 - 删除某个任务
,{alias_command("checkin")} pause 1 - 暂停某个任务
,{alias_command("checkin")} resume 1 - 恢复某个任务
,{alias_command("checkin")} status - 查看当前时间范围和任务列表
,{alias_command("checkin")} run 1 - 立即手动运行某个任务
,{alias_command("checkin")} run all - 立即手动运行所有任务
"""


async def from_msg_get_task_id(message: Message):
    uid = -1
    try:
        uid = int(message.parameter[1])
    except ValueError:
        await message.edit("请输入正确的参数")
        message.continue_propagation()
    ids = checkin_tasks.get_all_ids()
    if uid not in ids:
        await message.edit("该任务不存在")
        message.continue_propagation()
    return uid


@listener(
    command="checkin",
    parameters="start/end/add/rm/pause/resume/status/run",
    need_admin=True,
    description=f"在指定时间范围内随机发送签到消息\n请使用 ,{alias_command('checkin')} h 查看可用命令",
)
async def checkin(message: Message):
    if message.arguments == "h" or len(message.parameter) == 0:
        return await message.edit(checkin_help_msg)

    cmd = message.parameter[0].lower()

    if cmd == "status":
        text = f"当前签到时间范围：<code>{checkin_tasks.config.start_time}</code> ~ <code>{checkin_tasks.config.end_time}</code>\n\n"
        if checkin_tasks.get_all_ids():
            text += "已注册的签到任务：\n\n"
            text += checkin_tasks.print_all_tasks(show_all=True)
        else:
            text += "没有已注册的签到任务。"
        return await message.edit(text)

    if len(message.parameter) == 2:
        if cmd == "rm":
            if uid := await from_msg_get_task_id(message):
                checkin_tasks.remove(uid)
                checkin_tasks.save_to_file()
                checkin_tasks.load_from_file()
                return await message.edit(f"已删除任务 {uid}")
        elif cmd == "pause":
            if uid := await from_msg_get_task_id(message):
                checkin_tasks.pause_task(uid)
                return await message.edit(f"已暂停任务 {uid}")
        elif cmd == "resume":
            if uid := await from_msg_get_task_id(message):
                checkin_tasks.resume_task(uid)
                return await message.edit(f"已恢复任务 {uid}")
        elif cmd == "run":
            if message.parameter[1].lower() == "all":
                for task in checkin_tasks.get_all():
                    await checkin_tasks.send_message(task)
                return await message.edit("已手动运行所有任务")
            if uid := await from_msg_get_task_id(message):
                if task := checkin_tasks.get(uid):
                    await checkin_tasks.send_message(task)
                    return await message.edit(f"已手动运行任务 {uid}")

    if cmd == "start" and len(message.parameter) == 4 and message.parameter[2].lower() == "end":
        # checkin start 8:00 end 20:00
        try:
            checkin_tasks.update_config(message.parameter[1], message.parameter[3])
        except Exception as e:
            return await message.edit(f"参数错误：{e}")
        return await message.edit(
            f"已设置签到时间范围：<code>{checkin_tasks.config.start_time}</code> ~ <code>{checkin_tasks.config.end_time}</code>"
        )

    if cmd == "add":
        if len(message.parameter) < 3:
            return await message.edit("请输入目标用户名或 ID 和消息内容")
        target = message.parameter[1]
        msg = " ".join(message.parameter[2:]).strip()
        if not msg:
            return await message.edit("消息内容不能为空")
        try:
            cid = await resolve_cid(target, message)
        except Exception as e:
            return await message.edit(f"无法解析目标：{e}")
        task = CheckinTask(checkin_tasks.get_next_task_id(), cid=cid, msg=msg)
        checkin_tasks.add(task)
        checkin_tasks.register_single_task(task)
        checkin_tasks.save_to_file()
        checkin_tasks.load_from_file()
        return await message.edit(f"已添加签到任务 {task.task_id} -> <code>{cid}</code>")

    return await message.edit("请输入正确的参数")
