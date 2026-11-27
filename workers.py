"""
workers.py —— “干活的 AI”配置：秘书（本地 Ollama）把任务交给谁

秘书只负责听你说话、把任务转述清楚、交出去；真正干活的是这里列出的 AI。
以后想加新的 AI（比如 Gemini 出了更强的版本），或者换默认的，只改这个文件：
  1. 照着下面复制一段，改名字（WORKERS 里的键）、别名、类型和命令/网址
  2. DEFAULT_WORKER 改成你最常用的那个（你说“帮我做……”没点名时交给它）

两种类型：
  "cli"  命令行 AI（能在电脑上自己干活）：在任务文件夹里打开一个新窗口运行它，
         它会先读“任务说明.md”，然后自己完成任务，结果放在同一个文件夹
  "web"  网页 AI（只能聊天）：用浏览器打开网页，把任务复制到剪贴板，你按 Ctrl+V 发送
"""

import os
import sys

# 每个任务一个子文件夹。
TASKS_DIR = (r"D:\AI任务" if sys.platform == "win32" else
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "AI任务"))

# 没点名时交给谁
DEFAULT_WORKER = "Claude"

WORKERS = {
    "Claude": {
        # 别名：语音识别可能写成这些，秘书都能对上
        "aliases": ["Claude", "Cloud", "克劳德", "克劳"],
        "type": "cli",
        # --dangerously-skip-permissions = 最高权限：什么都不问直接做（你选的）
        "command": [r"%USERPROFILE%\.local\bin\claude.exe", "--dangerously-skip-permissions"],
        "about": "Claude Code，能在任务文件夹里自己写文件、写代码、运行程序、上网查资料",
    },
    "DeepSeek": {
        "aliases": ["DeepSeek", "深度求索", "迪普西克"],
        "type": "web",
        "url": "https://chat.deepseek.com/",
        "about": "DeepSeek 网页版（只能聊天）",
    },
    "豆包": {
        "aliases": ["豆包", "Doubao"],
        "type": "web",
        "url": "https://www.doubao.com/chat/",
        "about": "豆包网页版（只能聊天）",
    },
    "ChatGPT": {
        "aliases": ["ChatGPT", "GPT"],
        "type": "web",
        "url": "https://chatgpt.com/",
        "about": "ChatGPT 网页版（只能聊天）",
    },
    "Gemini": {
        "aliases": ["Gemini", "杰米尼", "双子座"],
        "type": "web",
        "url": "https://gemini.google.com/app",
        "about": "Gemini 网页版（只能聊天）",
    },
}
