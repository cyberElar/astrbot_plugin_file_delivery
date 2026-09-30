"""让「Agent 正在忙时发来的文件」真正送达 —— AstrBot 上游 bug 的临时补丁。

一句话：在适配器入口就把文件下载落盘，再把路径塞进插话文本，运行中的 Agent 当场
就能读到。下面记的是它为什么需要存在。

## 症状

Agent 正在一次运行中（active runner 未结束）时，同一发送者再发来的**文件**会整个
消失：Agent 上下文里只剩一行 `[ComponentType.File]`，磁盘（`data/temp`、
`data/attachments`、工作区）和数据库里都没有实体，事后无从找回。与文件大小无关，
唯一的变量是**发送时机** —— Agent 空闲时发就正常落盘为 `data/temp/fileseg_*`。

## 成因（AstrBot v4.28.2）

1. 事件被「插话」（follow-up）通道截走，而截走时**只留文本**：

       follow_up.py:193   runner.follow_up(message_text=_event_follow_up_text(event))
       follow_up.py:32    _event_follow_up_text() 先取 message_str，空则退回消息概要

   消息概要会把非文本组件折叠成占位符（`astr_message_event.py:144` 的
   `_outline_chain`），File ⇒ `[ComponentType.File]`，**不带文件名、不带 URL**。

2. 这条插话一旦被运行中的 Agent 消费，事件当场终止：

       internal.py:200-210  "Follow-up ticket already consumed, stopping processing" → return

3. 而文件下载**只发生在正常处理路径**上：

       astr_main_agent.py:1410  file_path = await comp.get_file()   # 落 data/temp/fileseg_*

   事件已经 return，这条路走不到 ⇒ 文件从未被下载。存档
   （`platform_message_history`）又是 path-free 的设计，所以事后无痕。

## 修法：入口物化 + 插话带路径

三步，缺一不可：

1. **入口物化**（`_install_ingress_materialization`）：在适配器唯一的漏斗
   `AiocqhttpAdapter.convert_message` 外面套一层，事件刚解析出来就把 File 组件
   下载到本地、写进 `file_`。

   放这里是因为**下载是异步的，而捕获钩子是同步的** —— `internal.py:194` 是
   `follow_up_capture = try_capture_follow_up(event)`，没有 await，在捕获那一步
   等不了下载。适配器的 `convert_message` 是 `async`，且被 request/notice/
   group/private 四条路都调用（`aiocqhttp_adapter.py:67/78/88/98`），是天然入口。

   顺带堵一个洞：排队策略会把下载推迟到拿到 session lock 之后，本轮要是跑很久，
   从 `get_group_file_url` 换来的那个签名 URL 可能已经过期。

2. **插话带上路径**（`_install_follow_up_text`）：跟进文本里附上上游同款格式的
   `[File Attachment: name …, path …]`。别以为改了组件就够了 ——
   `_event_follow_up_text()` 只要取到文本就**只返回文本**，文件信息整个丢掉，
   所以「给你 + 文件」这种消息必须在这一层补。

3. **没物化的仍然排队**（`_install_capture_guard`）：只有确认物化成功的 File 才
   允许进插话通道；没下下来的（下载失败、超时）走原来的排队路径，让正常处理再
   去取一次。Record/Video 没有物化，一律排队。

`get_file()` 开头就是「`file_` 已存在且文件在，直接返回绝对路径」
（`components.py:826`），所以入口预下载**不会导致重复下载** —— 后面正常路径拿到
的是同一个路径。

## 三个实现上的坑

**别用 `File.file`。** 它是同步 property，在异步上下文里会拒绝下载并打警告
（`components.py:791` 原话「不可以在异步上下文中同步等待下载!」）。必须
`await get_file()`。本插件只用 `isinstance` 和 `file_`，不碰它。

**捕获补丁要打在 `internal` 模块上，不是 `follow_up` 模块。** `internal.py:55-62`
是 `from ...follow_up import (... try_capture_follow_up ...)`，函数在导入那一刻
就被绑进了 `internal` 的命名空间，改源模块里的同名属性改不动它 —— 会静默失效。
（`_event_follow_up_text` 不一样：它在 `follow_up` 模块内部按全局名查找，所以要
打在 `follow_up` 上。）

**为什么是插件而不是覆盖容器里的文件。** 镜像 `soulter/astrbot:latest` 是滚动
tag，覆盖核心文件会在升级时丢失，或者版本对不上造成更难定位的故障。

## 边界

- `File` 在入口预下载；`Record`/`Video` 不预下载（一律排队）。
- **`Image` 既不预下载也不排队。** 它走 `MediaResolver`，没有 `get_file()` 那种
  「本地已有就短路」的性质，预下载省不下重复；而且群里图片太频繁，全量预下载不
  划算。代价是**插话里的图片仍然只有 `[图片]` 占位符** —— 已知限制。
- 预下载失败或超时不会让消息丢失，只会退回排队。
- `data/temp` 由 `TempDirCleaner` 按**总量**清理（默认上限 1GB，超了删最旧的
  30%，见 `temp_dir_cleaner.py:33-35`），不是按时间。磁盘紧张时，隔很久才去读
  那个路径有被清掉的可能。
- 上游修好后本插件即可删除。
"""

import asyncio
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Record, Video
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.permission import PermissionType
from astrbot.core.utils.media_utils import file_uri_to_path, is_file_uri

# 入口就预下载的组件类型。只放 File：它的 get_file() 有「本地已有就短路」的
# 语义，预下载不会白干。Image 为什么不在列，见 docstring 的「边界」。
INGRESS_MATERIALIZE = (File,)

# 这些组件一旦没能物化，消息就不能进插话通道 —— 概要一压就成了占位符，信息全没。
GUARDED_MEDIA = (File, Record, Video)

# 单个文件的下载上限。超了就退回排队，不拖着整条事件管线。
DOWNLOAD_TIMEOUT = 30

# 打在包装函数上的标记，用来识别「补丁是否已经生效」（热重载会重复走到这里）。
_PATCH_MARK = "_file_delivery_patched"

_STATUS: dict[str, str] = {}


def _chain(obj) -> list:
    """取消息链。传 event 或 AstrBotMessage 都行。"""
    msg_obj = getattr(obj, "message_obj", obj)
    return list(getattr(msg_obj, "message", None) or [])


def _local_path(comp) -> str | None:
    """组件已物化则返回绝对路径，否则 None。**同步** —— 所以只能读 file_，不能下载。"""
    raw = getattr(comp, "file_", "") or ""
    if not raw:
        return None
    path = file_uri_to_path(raw) if is_file_uri(raw) else raw
    return os.path.abspath(path) if os.path.exists(path) else None


async def _materialize(abm) -> None:
    for comp in _chain(abm):
        if not isinstance(comp, INGRESS_MATERIALIZE):
            continue
        if _local_path(comp):
            continue  # 已经落地了（比如同一条消息被复用）
        if not comp.url:
            continue  # 没有可下载的地址，交给后面的排队路径去报错
        try:
            await asyncio.wait_for(comp.get_file(), timeout=DOWNLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                f"[file_delivery] 下载超时（{DOWNLOAD_TIMEOUT}s），"
                f"该文件退回排队路径：{comp.name}"
            )
        except Exception:
            logger.warning(
                f"[file_delivery] 下载失败，该文件退回排队路径：{comp.name}",
                exc_info=True,
            )


def _install_ingress_materialization() -> str:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )

    original = AiocqhttpAdapter.convert_message
    if getattr(original, _PATCH_MARK, False):
        return "已生效（之前打过）"

    async def convert_message(self, event):
        abm = await original(self, event)
        if abm is not None:
            await _materialize(abm)
        return abm

    setattr(convert_message, _PATCH_MARK, True)
    AiocqhttpAdapter.convert_message = convert_message
    return "已生效"


def _attachment_lines(event) -> str:
    """把已物化的 File 拼成上游同款格式（astr_main_agent.py:1414），模型才认。"""
    lines = []
    for comp in _chain(event):
        if not isinstance(comp, File):
            continue
        path = _local_path(comp)
        if not path:
            continue
        name = comp.name or os.path.basename(path)
        lines.append(f"[File Attachment: name {name}, path {path}]")
    return "\n".join(lines)


def _install_follow_up_text() -> str:
    # 注意打的是 follow_up 模块：_event_follow_up_text 在模块内部按全局名查找，
    # 所以要改的是源模块（跟 try_capture_follow_up 正相反，理由见 docstring）。
    from astrbot.core.pipeline.process_stage import follow_up

    original = follow_up._event_follow_up_text
    if getattr(original, _PATCH_MARK, False):
        return "已生效（之前打过）"

    def _event_follow_up_text(event: AstrMessageEvent) -> str:
        text = original(event)
        attachments = _attachment_lines(event)
        if not attachments:
            return text
        # 原实现「有文本就只返回文本」，文件信息会整个丢掉 —— 这里补上。
        return f"{text}\n{attachments}" if text else attachments

    setattr(_event_follow_up_text, _PATCH_MARK, True)
    follow_up._event_follow_up_text = _event_follow_up_text
    return "已生效"


def _pending_media(event) -> str:
    """返回没能物化的媒体类型名，全都物化了就返回空串。"""
    for comp in _chain(event):
        if isinstance(comp, File):
            if not _local_path(comp):
                return "File"
        elif isinstance(comp, (Record, Video)):
            return type(comp).__name__
    return ""


def _install_capture_guard() -> str:
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal

    original = getattr(internal, "try_capture_follow_up", None)
    if original is None:
        return "❌ internal.try_capture_follow_up 不存在（AstrBot 改结构了？）"
    if getattr(original, _PATCH_MARK, False):
        return "已生效（之前打过）"

    def try_capture_follow_up(event: AstrMessageEvent):
        pending = _pending_media(event)
        if pending:
            logger.info(
                f"[file_delivery] {pending} 未物化，不并入插话，改走正常路径"
            )
            return None
        return original(event)

    setattr(try_capture_follow_up, _PATCH_MARK, True)
    internal.try_capture_follow_up = try_capture_follow_up
    return "已生效"


def install() -> dict[str, str]:
    """装三个补丁并返回各自的状态。幂等，重复调用无副作用。

    单独抽成模块级函数是为了让 test_offline.py 不构造 Context 也能测。
    """
    _STATUS.update(
        入口物化=_install_ingress_materialization(),
        插话带路径=_install_follow_up_text(),
        未物化则排队=_install_capture_guard(),
    )
    return _STATUS


@register(
    "file_delivery",
    "Elarian",
    "修复 Agent 运行中发来的文件被插话通道吞掉、从未下载的问题（上游 bug 的临时补丁）。",
    "2.0.0",
)
class FileDelivery(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        logger.info(
            "[file_delivery] "
            + "；".join(f"{k}={v}" for k, v in install().items())
        )

    @filter.command("filedelivery")
    @filter.permission_type(PermissionType.ADMIN)
    async def filedelivery(self, event: AstrMessageEvent):
        """看一眼三个补丁是不是都挂上了。"""
        yield event.plain_result(
            "file_delivery\n"
            + "\n".join(f"  {k}：{v}" for k, v in _STATUS.items())
            + f"\n入口预下载：{', '.join(t.__name__ for t in INGRESS_MATERIALIZE)}"
            f"\n未物化则排队：{', '.join(t.__name__ for t in GUARDED_MEDIA)}"
        )
