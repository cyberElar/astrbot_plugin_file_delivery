"""离线自测：不起 AstrBot、不连 QQ、不发消息，直接验证三个补丁的行为。

跑法见 README。**必须把 `ASTRBOT_ROOT` / `ASTRBOT_CONFIG_PATH` 重定向掉** ——
光 `import astrbot` 就会走到 `AstrBotConfig.__init__` 往 `data/cmd_config.json`
写盘（细节见 `plugins/qq_api/README.md`），那是正在跑的实例的配置。

不联网：入口预下载那步只验证「已有本地文件时会不会被认成已物化」，不会真去下载。
假 event 带了 `get_message_str` / `get_message_outline`，所以**插话文本走的是真
实现**（`follow_up._event_follow_up_text`），不是 stub 出来的。
"""

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

FAILED: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"{'✅' if ok else '❌'} {name}  →  got={got!r} want={want!r}")
    if not ok:
        FAILED.append(name)


def make_event(segments, text: str = "") -> types.SimpleNamespace:
    """够三个补丁用：它们只读 message_obj.message、file_，以及文本与概要。"""
    ev = types.SimpleNamespace()
    ev.message_obj = types.SimpleNamespace(message=segments)
    ev.get_message_str = lambda: text
    ev.get_message_outline = lambda: "[ComponentType.File]"
    return ev


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "followup_media_guard_main", Path(__file__).with_name("main.py")
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    from astrbot.api.message_components import File, Image, Plain, Record, Video
    from astrbot.core.pipeline.process_stage import follow_up
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
        AiocqhttpAdapter,
    )

    # 一个真实存在的本地文件，用来把 File 组件伪装成「入口已物化」
    tmpdir = tempfile.mkdtemp()
    landed = os.path.join(tmpdir, "报告.txt")
    Path(landed).write_text("hi")
    attachment = f"[File Attachment: name 报告.txt, path {landed}]"

    real = (
        internal.try_capture_follow_up,
        follow_up._event_follow_up_text,
        AiocqhttpAdapter.convert_message,
    )

    # 哨兵顶替真的 capture：一眼看出「有没有委派下去」
    calls: list = []

    def sentinel(event):
        calls.append(event)
        return "SENTINEL"

    internal.try_capture_follow_up = sentinel

    mod = load_plugin()

    try:
        status = mod.install()
        check("三个补丁都报已生效", set(status.values()), {"已生效"})

        patched_capture = internal.try_capture_follow_up
        patched_text = follow_up._event_follow_up_text
        patched_convert = AiocqhttpAdapter.convert_message
        check("入口物化替换了适配器方法", patched_convert is not real[2], True)

        mod.install()
        check(
            "幂等：重复安装不套娃",
            (
                internal.try_capture_follow_up is patched_capture,
                follow_up._event_follow_up_text is patched_text,
                AiocqhttpAdapter.convert_message is patched_convert,
            ),
            (True, True, True),
        )

        # ---- 插话文本：已物化的 File 要带上路径，格式与上游 astr_main_agent.py:1414 一致
        check(
            "有文字时：文本 + 路径",
            patched_text(make_event([Plain(text="给你"), File(name="报告.txt", file=landed)], text="给你")),
            f"给你\n{attachment}",
        )
        # 纯文件消息：上游概要本来就会给一行 [ComponentType.File] 占位符，
        # 插件只在它后面追加路径，不去动原来的东西。
        check(
            "纯文件时：占位符之后追加路径",
            patched_text(make_event([File(name="报告.txt", file=landed)])),
            f"[ComponentType.File]\n{attachment}",
        )
        check(
            "没有文件时不追加任何东西",
            patched_text(make_event([Plain(text="在吗")], text="在吗")),
            "在吗",
        )

        # ---- 排队守卫：已物化 → 放行插话；没物化 → 退回排队
        calls.clear()
        ev_landed = make_event([File(name="报告.txt", file=landed)])
        check("已物化的 File 放行进插话", patched_capture(ev_landed), "SENTINEL")
        check("  └ 原函数被调用", len(calls), 1)

        calls.clear()
        check(
            "没物化的 File 退回排队",
            patched_capture(make_event([File(name="b.txt", url="https://x/b.txt")])),
            None,
        )
        check("  └ 原函数没被调用", len(calls), 0)

        calls.clear()
        check("Record 一律排队", patched_capture(make_event([Record(file="a.mp3")])), None)
        check("Video 一律排队", patched_capture(make_event([Video(file="v.mp4")])), None)
        check("  └ 都没委派下去", len(calls), 0)

        calls.clear()
        check(
            "纯文本照旧进插话",
            patched_capture(make_event([Plain(text="在吗")], text="在吗")),
            "SENTINEL",
        )
        check(
            "Image 照旧进插话（已知限制，见 README「边界」）",
            patched_capture(make_event([Image(file="a.jpg")])),
            "SENTINEL",
        )

        # ---- 零散健壮性
        asyncio.run(mod._materialize(types.SimpleNamespace(message=[File(name="c.txt")])))
        check("_materialize 对无 url 的 File 不抛异常", True, True)
        check("_attachment_lines 对畸形输入返回空", mod._attachment_lines(types.SimpleNamespace()), "")
        check("_pending_media 对畸形输入返回空", mod._pending_media(types.SimpleNamespace()), "")
    finally:
        (
            internal.try_capture_follow_up,
            follow_up._event_follow_up_text,
            AiocqhttpAdapter.convert_message,
        ) = real

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项未通过：{FAILED}")
        return 1
    print("✅ 全部通过（三个补丁均已还原）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
