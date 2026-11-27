# 星期八（Xingqiba）— 一座会说话的“桥”

> 24 小时在家里听着，听懂你要什么，转给最擅长的 AI 或设备去做，再用语音把结果说回来。

**English summary:** Xingqiba ("Friday + 1") is a personal, local-first Chinese voice assistant for Windows and macOS. It listens for a wake word, transcribes Mandarin offline (sherpa-onnx), thinks with a local LLM via Ollama, and speaks back. Its real job is to be a *bridge*: a local model handles reflexes (volume, reminders, routing) while hard tasks are handed to stronger AIs (Claude / GPT) through a built-in multi-AI group chat. Early-stage hobby project — ideas and feedback very welcome.

---

## 为什么做它

- 想让**爸妈、老人**也能开口就用上 AI：不用打字、不用调设置，出错有人话提示。
- **省钱**：日常的听、说、简单的事全部在本机跑，免费；只有复杂的事才交给 Claude / GPT。
- **不造轮子**：点外卖、购物、智能家居都交给现成的服务或 AI，星期八只做“听懂 → 转给对的人 → 说回来”这一层。

详细思路见 [docs/桥梁设计.md](docs/桥梁设计.md)。

## 现在能做什么

| 部分 | 状态 |
|---|---|
| 唤醒词“星期八”，离线关键词检测 + 二次确认 | ✅ |
| 普通话离线识别：Paraformer 流式出字 + SenseVoice 定稿 | ✅ |
| 断句（说话慢也等你）、连着说的指令不丢（“星期八，把音量调大”） | ✅ |
| 它说话时可以打断（WebRTC 回声消除） | ✅ |
| 本地大脑：Ollama 上的 Gemma 4（Windows 12B / Mac E4B），可换其他模型 | ✅ |
| 语音回复：在线（微软 Edge 神经语音）或离线（Kokoro） | ✅ |
| 调度员：一段话拆成几件事，派给对应的成员 | ✅ |
| 定时 / 带条件的待办（“四十分钟后提醒我吃药”“洗完澡后开电视”） | ✅ |
| AI 群聊：Claude、GPT（用你自己的账号登录）、本地模型、DeepSeek/豆包/千问等兼容 OpenAI 接口的服务 | ✅ |
| 网页界面（本机 127.0.0.1）+ 托盘 / 菜单栏图标 | ✅ |
| 操作电脑（打开网页、读窗口、点按钮） | Windows ✅ / Mac ❌ |
| MiniCPM-o 4.5 全双工对话（边听边说） | Windows + NVIDIA ✅ / Mac ❌ |

还没做、正在想的：通用浏览器操作、技能“越用越熟”（重复的事自动沉淀成技能）、接入小爱音箱当耳朵、Home Assistant 智能家居、给老人用的手机大字入口。

## 安装

> 本仓库**只有代码**，不含任何模型。本地大模型、语音模型请按下面的说明自己下载。

### 1. 通用准备

1. 安装 **Python**：Windows 用 3.11；Mac 测过 3.13。
2. 安装 **[Ollama](https://ollama.com)**，拉一个本地模型（默认名字写在 `local_models.py`）：
   ```bash
   ollama pull gemma4:12b   # Windows（显卡 12GB 左右）
   ollama pull gemma4:e4b   # Mac 16GB 内存推荐
   ```
   想换别的模型，改 `local_models.py` 里的 `REASONING_MODELS`。
3. 按 [models/README.md](models/README.md) 下载语音模型，放进 `models/` 文件夹。

### 2. Windows

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py --console   # 带黑窗口，方便看过程
```
- 平时用 `pythonw app.py` 在后台运行，托盘里会出现“八”字图标。
- `python setup_shortcuts.py` 可以生成开始菜单快捷方式。

### 3. Mac（Apple 芯片）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-mac.txt
bash make_mac_app.sh        # 生成 /Applications/星期八.app
```
- 双击“星期八.app”启动，菜单栏会出现“八”字图标。第一次启动要允许麦克风。
- 详见 [docs/Mac.md](docs/Mac.md)。

### 4. 可选：接 Claude / GPT

- **Claude**：装 Claude Code 并登录自己的账号，群聊和“交给 Claude”就能用会员额度。
- **GPT**：装 Codex 并登录 ChatGPT 账号。
- 其他兼容 OpenAI 接口的服务（DeepSeek、豆包、千问、Kimi、GLM、Gemini……）：在界面的群聊成员里填密钥。

第一次运行会在项目里生成 `api_keys.json`、`settings.json`、`memory/`、`groupchat/`、`logs/`。这些是**你的私人数据**，已经写进 `.gitignore`，不要上传。

## 项目结构（主要文件）

| 文件 | 作用 |
|---|---|
| `app.py` | 程序入口：托盘 / 菜单栏、网页界面、语音主循环 |
| `listen.py` | 唤醒 → 录音 → 识别 → 对话窗口 |
| `wakeword.py` / `transcription.py` / `speech_endpoint.py` | 唤醒词、语音识别、断句 |
| `duplex.py` / `speak.py` | 回声消除与打断、语音合成与播放 |
| `assistant.py` / `local_models.py` | 本地大脑（Ollama）和工具调用 |
| `bridge.py` / `bridge_pending.py` | 调度员：拆任务、派活、定时和条件待办 |
| `groupchat.py` 等 `group_*.py` | AI 群聊 |
| `computer.py` / `desktop_ctl.py` | 操作电脑（Windows） |
| `minicpm_*.py` | MiniCPM-o 全双工语音（Windows + NVIDIA） |
| `ui/` | 网页界面 |

测试：`python -m unittest test_bridge test_bridge_pending test_wakeword ...`（逐个文件跑；有几个 `test_*.py` 是会真的操作电脑的脚本式测试，不要一次全量自动发现）。

## 它是一座桥，不是一个大脑

星期八**不打算自己变成最聪明的 AI**。大模型每个月都在变强，今天是 Claude、GPT，明天可能是 Muse、豆包或者别的什么；不管谁最强，只要它是个大模型，就当成一个新成员接到桥上。

星期八自己只做三件事：

1. **听**：24 小时在家里收音，把你说的话听准（唤醒、识别、断句、打断）。
2. **转**：听懂你要什么，交给最擅长的那一个——家电、外卖 App、购物网站、Claude、GPT……
3. **说回来**：把结果、对方的追问用语音念给你，你的回答再转回去。

```
          你说话
            │
   ┌────────▼────────┐
   │  星期八（本机）   │  耳朵 + 嘴 + 接线员：免费、24 小时
   └───┬────┬────┬───┘
       │    │    │
   家电/App  GPT  Claude  ……以后的任何 AI
```

换模型、加成员，桥本身不用动。

## 我现在的想法，和卡住的地方

这是一个人 + AI 一起做的业余项目（代码主要由 Claude 和 GPT 协作写成），很多地方还很粗糙。下面是我现在的思路，以及自己想不通的地方，**特别欢迎大家给建议**。

### 1. 本地模型太“笨”，但又不想变成“小爱同学”

- 实际例子：我说“打开京东，搜索 Mac Mini”，本地的 Gemma 4 12B 直接去搜索引擎里搜了 Mac Mini，而不是在京东站内搜。
- 一种解法是给每类事写固定的工作流（京东搜索模板、淘宝搜索模板……）。这样又快又稳，但**全靠工作流，就成了只会固定指令的小爱同学**，不是真正的 AI 介入生活。
- 另一种是给 AI 通用的“手”（能看网页、找输入框、打字、点击），让它自己想办法。这样足够灵活，但 12B 这种小模型做多步操作经常想岔。

**卡住的地方**：本地模型能力有限，云端大模型又贵、又慢一点。两者的边界该怎么划？

### 2. 我设想的分工：接线员 → 大脑 → 执行者

```
你说话 → Gemma（本地接线员）
            ├─ 闲聊、调音量、定提醒 → 自己马上办
            └─ 其他 → 转述给 GPT（大脑）
                         ├─ 简单的 → 直接回答 → 念给你
                         └─ 复杂的 → 写清楚步骤 → 交给 Claude（执行者）去做
```

就像现在的 AI 群聊：一个负责转述我的话，一个负责想，一个负责动手。

**还没想清楚的**：
- GPT / Claude 一来一回要好几秒，语音对话里怎么让等待不那么难受？
- 转述的时候要带多少上下文，比如“就买刚才那个”里的“刚才那个”？
- 谁来决定“这件事简单还是复杂”？

### 3. 越用越好用：让重复的事自动变成“技能”

- 一开始大家都陌生，大部分话都要交给 GPT 去理解。
- 用几天以后，GPT 和 Claude 定期复盘：某类事反复出现、步骤都一样，就写成一个“技能”（工作流），比如调音量、打字、开关灯。
- 以后说到这类事，**直接由本地触发技能，不用再经过 GPT 解析**；只有真正复杂、每次都不一样的事（开网页办事、点外卖）才交给大模型。

**卡住的地方**：
- **触发规则**：怎样让本地快速、准确地认出“这句话就是那个技能”，换个说法也能认出来，又不会把别的话误认成技能？
- 技能学错了、网站改版失灵了，怎么自动发现、自动修？
- 学新技能要不要每次都请主人批准？

### 4. 能不能直接借用现成的小爱同学？

- 小爱音箱收音好、便宜、老人熟悉，自带的开灯、闹钟、放歌都做得很好。
- 想法：让小爱当“耳朵和嘴”，它会的照常自己做；它不会的转给家里的主机（星期八），再由 GPT / Claude 处理，最后通过小爱音箱念出来。
- 社区里有非官方的接入方式（读取小米云的对话记录、再让音箱念回答），但可能随着小米更新失效，而且小米没有开放修改小爱工作流的接口。

**卡住的地方**：这条路长期可行吗？有没有更稳的“全屋耳朵”方案（ESP32 小智类硬件、Home Assistant 语音卫星、旧手机）？

### 5. 给爸妈、老人用

- 目标是让爸妈那一辈开口就能用上 AI：不用打字、不用调设置、出错时有人话提示。
- 他们更适合手机上大字 + 语音的简单入口，而不是电脑界面。
- 群聊里接入真人（比如爸妈）之前，需要先做“按人分权限”：家人只能聊天提问，动手操作只有主人能批准。

**卡住的地方**：怎样才能真正做到“老人用不会卡住”？

### 6. 其他

- 中文唤醒、断句、回声消除，有没有更好的开源方案？
- Mac 16GB 内存跑 12B 太慢，现在用小一号的 E4B。小内存设备上的本地模型怎么选更好？

欢迎提 Issue 或 Pull Request，哪怕只是一句想法也很感谢。

## 许可证和第三方

- 本仓库代码的许可证见 `LICENSE` 文件。
- 各模型（Gemma、SenseVoice、Paraformer、Kokoro、Whisper、MiniCPM-o 等）都有各自的许可证，需要你自己下载并遵守。
- 在线语音使用微软 Edge 的语音服务（通过 `edge-tts`）。
