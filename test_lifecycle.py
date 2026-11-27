"""
test_lifecycle.py —— 测试“按需加载”：待命时不占显存，唤醒时加载，用完卸载后显存还回去

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_lifecycle.py
"""

import subprocess
import time

import ollama

import assistant
import listen

results = []


def vram():
    """显卡当前用了多少显存（MB）。"""
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip().splitlines()[0])


def gemma_loaded():
    return any(m.model.startswith(assistant.MODEL) for m in ollama.ps().models)


def check(name, ok):
    results.append(ok)
    print(f"  {'[通过]' if ok else '[失败]'} {name}")


# 1. 待命：两个模型都没加载
assistant.unload()
time.sleep(3)
stt = listen.SpeechToText(load=False)
base = vram()
print(f"待命：显存 {base} MB")
check("待命时 gemma4 没有加载", not gemma_loaded())
check("待命时语音识别模型没有加载", not stt.loaded)

# 2. 唤醒：后台同时加载两个模型
start = time.time()
stt.load_in_background()
assistant.load_in_background()
while not (stt.loaded and gemma_loaded()) and time.time() - start < 60:
    time.sleep(0.2)
loaded_after = time.time() - start
time.sleep(1)
used = vram()
print(f"唤醒后：两个模型都加载好用了 {loaded_after:.1f} 秒，显存 {used} MB（+{used - base} MB）")
check("唤醒后两个模型都加载好了", stt.loaded and gemma_loaded())

# 3. 复盘完：语音识别模型“停”到内存里（默认设置 WHISPER_IN_STANDBY = "ram"）
assistant.unload()
time.sleep(2)
before_park = vram()
stt.park()
time.sleep(1)
parked = vram()
print(f"把语音识别模型停到内存里：显存释放了 {before_park - parked} MB")
check("停到内存后释放了 1.5GB 以上显存", before_park - parked > 1500)
t = time.time()
stt.load()
back = time.time() - t
check(f"从内存挪回显卡少于 0.5 秒（实际 {back:.2f} 秒）", back < 0.5)

# 4. 退出时全部卸载
stt.unload()
time.sleep(3)
after = vram()
print(f"卸载后：显存 {after} MB（比待命时 {after - base:+d} MB）")
check("卸载后 gemma4 不在显存里了", not gemma_loaded())
check("卸载后显存基本回到待命时的水平（相差 500MB 以内）", abs(after - base) < 500)

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
