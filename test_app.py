"""
test_app.py —— 从头到尾测一遍“植入”版：
    双击桌面快捷方式启动 → 在后台待命（没有窗口、不占显存）→ 用关闭脚本彻底关闭

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_app.py
注意：会关掉正在运行的命令行版 listen.py（后台版启动时就会这样做）。
"""

import os
import subprocess
import sys
import time

import ollama

import assistant
import housekeeping
import setup_shortcuts

APP = os.path.join(setup_shortcuts.PROJECT_DIR, "app.py")
LISTEN = os.path.join(setup_shortcuts.PROJECT_DIR, "listen.py")
ERRORS = os.path.join(setup_shortcuts.PROJECT_DIR, "logs", "errors.log")
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


def vram():
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


def gemma_loaded():
    return any(m.model.startswith(assistant.MODEL) for m in ollama.ps().models)


log_start = os.path.getsize(ERRORS) if os.path.exists(ERRORS) else 0
if housekeeping.find_instances(APP) or housekeeping.find_instances(LISTEN):
    # 你正在用的助手不能被测试关掉（会打断你、还会触发复盘写进你真正的记忆）
    print("[跳过] 语音助手正在运行（你可能正在用），这个测试会把它关掉，所以先不测。")
    print("       想测的话：先右键托盘图标 → 退出，再运行这个测试。")
    print("\n结果：0/0 项通过（已跳过）")
    sys.exit(0)
assistant.unload()  # 之前的测试可能让大模型留在显存里，先卸掉，从干净的状态开始
time.sleep(3)

print("===== 1. 双击桌面快捷方式启动 =====")
shortcut = setup_shortcuts.shortcut_paths()[0][0]
base = vram()
os.startfile(shortcut)
t = time.time()
while not housekeeping.find_instances(APP) and time.time() - t < 30:
    time.sleep(0.5)
check("后台版启动了", bool(housekeeping.find_instances(APP)), f"{time.time() - t:.1f} 秒")
time.sleep(15)  # 等它加载完语音识别、进入待命
procs = housekeeping.find_instances(APP)
check("15 秒后还在稳定运行", bool(procs))
check("用的是不弹窗口的 pythonw", all(p.name().lower() == "pythonw.exe" for p in procs),
      f"进程：{[p.name() for p in procs]}")
check("命令行版 listen.py 已经被关掉，不会抢麦克风", not housekeeping.find_instances(LISTEN))
check("待命时大模型没有加载", not gemma_loaded())
check("待命时显存增加不到 0.5GB", vram() - base < 500, f"增加了 {vram() - base} MB")

print("\n===== 2. 运行“关闭”彻底退出 =====")
t = time.time()
out = subprocess.run([sys.executable, "stop.py", "--quiet"], cwd=setup_shortcuts.PROJECT_DIR,
                     capture_output=True, text=True, encoding="utf-8").stdout.strip()
seconds = time.time() - t
check("后台版已经退出", not housekeeping.find_instances(APP), f"用时 {seconds:.1f} 秒（{out}）")
check("是自己体面退出的，不是被强制关的", "强制" not in out)
check("退出后大模型不在显存里", not gemma_loaded())

with open(ERRORS, encoding="utf-8") as f:
    f.seek(log_start)
    new_log = f.read()
check("运行期间没有出错", "Traceback" not in new_log, f"日志：{new_log.strip().splitlines()}")

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
