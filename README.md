# followup_media_guard

修补 AstrBot 的一个 bug：**助手正在运行中时发来的文件会整个消失**。

> 这是给上游 bug 打的临时补丁。上游修好后，把本插件和 compose 里那行挂载一起删掉即可。

## 症状

助手某一轮还没结束（正在跑工具调用循环）时，同一发送者再发一个**文件**：

- 助手那边只看到一行 `[ComponentType.File]` —— 没有文件名，也没有链接；
- 文件**从未被下载**：`data/temp`、`data/attachments`、工作区和数据库里都没有实体；
- 事后找不回来（存档是 path-free 的）。

与文件大小无关（4 字节的也照样丢），唯一的变量是**发送时机** —— 助手空闲时发就正常落盘为 `data/temp/fileseg_*`，并以 `[File Attachment: name …, path …]` 进入上下文。

## 成因（AstrBot v4.28.2）

1. 事件被「插话」（follow-up）通道截走，而截走时**只留文本**：

   ```
   follow_up.py:193   runner.follow_up(message_text=_event_follow_up_text(event))
   follow_up.py:32    _event_follow_up_text() 先取 message_str，空则退回消息概要
   ```

   消息概要会把非文本组件折叠成占位符（`astr_message_event.py:144` 的 `_outline_chain`），File ⇒ `[ComponentType.File]`。

2. 这条插话一旦被运行中的助手**消费**，事件当场终止：

   ```
   internal.py:200-210  "Follow-up ticket already consumed, stopping processing" → return
   ```

3. 而文件下载**只发生在正常处理路径**上：

   ```
   astr_main_agent.py:1410  file_path = await comp.get_file()   # 落 data/temp/fileseg_*
   ```

   事件已经 `return`，这条路走不到 ⇒ 文件从未被下载。

## 修法：入口物化 + 插话带路径

**入口就下载，然后把路径塞进插话文本** —— 运行中的助手当场就能 `Read` 那个文件，不用等本轮结束。

三个补丁，缺一不可：

| 补丁 | 打在哪 | 干什么 |
|---|---|---|
| 入口物化 | `AiocqhttpAdapter.convert_message` | 事件刚解析出来就下载 File、写进 `file_` |
| 插话带路径 | `follow_up._event_follow_up_text` | 跟进文本里追加 `[File Attachment: name …, path …]` |
| 未物化则排队 | `internal.try_capture_follow_up` | 没下下来的文件不进插话，退回排队 |

**为什么要放在适配器。** 因为下载是异步的，而捕获钩子是**同步**的 —— `internal.py:194` 是 `follow_up_capture = try_capture_follow_up(event)`，没有 `await`，在捕获那一步根本等不了下载。`convert_message` 是 `async`，且被 request/notice/group/private 四条路都调用（`aiocqhttp_adapter.py:67/78/88/98`），是天然的入口。

**顺带堵了一个洞。** 早期版本只让文件排队（不物化），那会把下载推迟到拿到 session lock 之后 —— 本轮要是跑很久，从 `get_group_file_url` 换来的**签名 URL 可能已经过期**，正常路径再去下载照样失败。本版本在入口就把字节拿到手，这个问题不存在了。

**不会重复下载。** `get_file()` 开头就是「`file_` 已存在且文件在，直接返回绝对路径」（`components.py:826`），所以入口预下载之后，正常路径拿到的是同一个路径。若插话**没被消费**（本轮先结束了），事件会继续走正常路径，文件也是一样送达 —— 而且只送达一次。

## 装法

靠 compose 里这一行挂进容器（跟 `qq_api` 一样，插件要**逐个挂**）：

```yaml
      - ./plugins/followup_media_guard:/AstrBot/data/plugins/followup_media_guard
```

挂完重启 astrbot。启动日志里出现这行就算生效：

```
[followup_media_guard] 入口物化=已生效；插话带路径=已生效；未物化则排队=已生效
```

在聊天里发 `/followupguard`（仅管理员）也能随时复查三个补丁的状态。

## 实现上的三个坑

**别用 `File.file`。** 它是同步 property，在异步上下文里会拒绝下载并打警告，源码原话（`components.py:791`）：

> 不可以在异步上下文中同步等待下载! ... 请使用 `await get_file()` 代替直接获取 `<File>.file` 字段

**两个补丁要打在**不同**的模块上。** `try_capture_follow_up` 要打在 `internal` 模块上 —— `internal.py:55-62` 是 `from ...follow_up import (...)`，函数在导入那一刻就被绑进了 `internal` 的命名空间，改源模块里的同名属性**改不动它**，补丁会静默失效。而 `_event_follow_up_text` 正相反：它在 `follow_up` 模块内部按全局名查找，所以必须打在 `follow_up` 上。**搞反了不会报错，只会不生效。**

**为什么是插件而不是覆盖容器里的文件。** 镜像 `soulter/astrbot:latest` 是滚动 tag，覆盖核心文件会在镜像升级时丢失，或者版本对不上造成更难定位的故障。

## 边界

- **`File` 在入口预下载**；`Record` / `Video` 不预下载，一律排队（和以前一样）。
- **`Image` 既不预下载也不排队** —— 仍走插话，文本里还是 `[图片]` 占位符。原因：它走 `MediaResolver`，没有 `get_file()` 那种「本地已有就短路」的性质，预下载省不下重复；而且群里图片太频繁，全量预下载不划算。**这是已知限制**：运行中的助手看不到插话里的图片。要改的话，把 `main.py` 的 `INGRESS_MATERIALIZE` 加上 `Image` 即可，但要接受图片全量预下载的代价。
- **预下载失败或超时（30s）不会让消息丢失**，只会退回排队，让正常处理再试一次。
- 纯文件消息的插话文本会长这样 —— 上游概要那行 `[ComponentType.File]` 占位符还在，插件只在它后面追加路径，不去动原有内容：

  ```
  [ComponentType.File]
  [File Attachment: name 报告.txt, path /AstrBot/data/temp/fileseg_报告_ab12cd34.txt]
  ```

- `data/temp` 由 `TempDirCleaner` 按**总量**清理，不是按时间：默认上限 1GB，超了删最旧的 30%（`temp_dir_cleaner.py:33-35`）。磁盘紧张时，隔很久才去读那个路径有被清掉的可能。

## 自测

`test_offline.py` 不连 QQ、不起 AstrBot、不联网，验证三个补丁拦没拦住、该委派的有没委派、文本拼得对不对（19 项）：

```bash
./scripts/dock.sh "docker exec astrbot mkdir -p /tmp/fakeroot /tmp/fmg_check"
./scripts/dock.sh "docker cp plugins/followup_media_guard/main.py astrbot:/tmp/fmg_check/main.py"
./scripts/dock.sh "docker cp plugins/followup_media_guard/test_offline.py astrbot:/tmp/fmg_check/test_offline.py"
./scripts/dock.sh "docker exec -e ASTRBOT_ROOT=/tmp/fakeroot -e ASTRBOT_CONFIG_PATH=/tmp/fakeroot/cmd_config.json astrbot python3 /tmp/fmg_check/test_offline.py"
```

> ⚠️ **那两个 `-e` 不能省。** `import astrbot` 会走到 `AstrBotConfig.__init__`，它一初始化就往 `data/cmd_config.json` 写盘。不重定向就会改到正在跑的实例的配置（详见 `plugins/qq_api/README.md`）。
