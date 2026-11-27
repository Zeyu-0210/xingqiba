# 在 Mac 上使用星期八

## 准备和启动

1. 在 Apple 芯片 Mac 上打开终端，进入项目目录。使用原生 ARM64 的 Python 3.13，在 Mac 上创建自己的 `.venv`。从 Windows 复制项目时，在 Mac 目录里省略 Windows 的 `.venv`。

   ```bash
   python3.13 -m venv .venv
   .venv/bin/python -m pip install -r requirements-mac.txt
   chmod +x start_mac.command stop_mac.command
   ```

2. 启动 Ollama，用 `ollama list` 确认已有 `gemma4:e4b`（Mac 默认，16GB 内存上比 12B 快 4 倍多；想用 12B 可以在设置里换）。保留 `models/wakeword_keywords.txt` 和唤醒词、SenseVoice、Paraformer、Kokoro 的模型目录。若要使用 Whisper 备用转写，也需要 `models/faster-whisper-large-v3-turbo`。
3. 双击 `start_mac.command`。星期八在后台运行，并用默认浏览器打开界面。菜单栏里的「八」可以打开界面、暂停监听或退出。第一次启动时，按 macOS 提示允许访问麦克风。也可以到 **系统设置 → 隐私与安全性 → 麦克风** 检查终端或 Python 的权限。
4. 双击 `stop_mac.command`，或在菜单栏选择「退出（彻底关闭）」。启动和错误记录分别在 `logs/mac-start.log` 和 `logs/errors.log`。
5. **做成软件**：在项目文件夹运行一次 `bash make_mac_app.sh`，会生成「应用程序」里的 `星期八.app`。以后在启动台、聚焦搜索或程序坞里双击它就能启动，不弹终端窗口，只在菜单栏显示「八」。App 只是启动器，改了代码不用重新打包；项目文件夹挪了位置才需要重新运行。第一次用 App 启动时，macOS 会以「星期八」的名义再问一次麦克风权限。

## 可以使用的功能

- 说「星期八」唤醒，使用 SenseVoice 转写语音指令，由 Ollama 的 Gemma 回答，再用离线 Kokoro 或联网的 edge-tts 播报。群聊语音输入使用流式 Paraformer 和 SenseVoice。需要 Whisper 备用转写时，按需加载 CPU int8 模型。
- 可语音查询、调整或静音系统音量。回声消除使用程序声音作为参考，能否稳定插话取决于麦克风、扬声器和回声消除组件。
- 浏览器里的打字对话、AI 群聊、调度员、挂起任务和到点提醒可用。若要在群聊中使用 Claude Code 或 Codex，可提前在终端登录相应账号。

## 首版暂不可用的功能

- 桌面操作、启动外部窗口的任务委派、Claude 接管及群聊执行权。群成员可以继续回答和讨论。
- MiniCPM-o 全双工模式、WASAPI 系统回放参考、悬浮状态窗、自动启动和夜间研究。

## Mac 实机复查

- 从 Finder 双击启动，检查菜单栏、浏览器和启动静音，再试暂停、恢复和退出。
- 使用内置麦克风与扬声器，分别测试 SenseVoice 和 Paraformer、Kokoro 和 edge-tts、音量和插话。
- 在群聊发一条调度指令，设置一项短时提醒，确认到点有群消息和播报。

## 改动文件

- 启停和界面涉及 `app.py`、`appicon.py`、`ui_server.py`、`ui/app.js`、`housekeeping.py`、`setup_shortcuts.py`、`stop.py`。
- 语音与回声涉及 `listen.py`、`transcription.py`、`volume.py`、`duplex.py`、`local_models.py`、`cuda_libs.py`、`minicpm_engine.py`、`minicpm_voice.py`。
- 群聊、工具和命令行涉及 `groupchat.py`、`assistant.py`、`computer.py`、`desktop_ctl.py`、`delegate.py`、`agent_hands.py`、`brains.py`、`takeover.py`、`agent_codex.py`、`brain_claude_code.py`、`workers.py`、`research.py`。
- 新增文件为 `requirements-mac.txt`、`start_mac.command`、`stop_mac.command`、`docs/Mac.md`、`test_mac.py`。
