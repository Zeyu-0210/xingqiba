"""
test_status.py —— 测试状态显示

    1. 状态广播：每一步都能通知到“听众”
    2. 用时日志：记下每一步用了几秒，但不记你说的话、不记助手的回答
    3. 状态窗：真的打开它，切换几种状态并截图（截图存在 tests\\_overlay_shots，看完可以删）
    4. 状态窗的自动隐藏：回到待命 3 秒后隐藏

运行方式（在项目文件夹 D:\\voice-assistant 里）：
    .venv\\Scripts\\python.exe test_status.py
注意：测试时屏幕右下角会出现几秒状态窗。
"""

import os
import shutil
import time

from PIL import ImageGrab

import overlay
import status

results = []
SHOTS = os.path.join(status.PROJECT_DIR, "tests", "_overlay_shots")
TMP_LOG = os.path.join(status.PROJECT_DIR, "tests", "_tmp_status_logs")


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'[通过]' if ok else '[失败]'} {name}  {detail}")


print("===== 1~2. 状态广播和用时日志 =====")
heard = []
status.subscribe(lambda state, detail: heard.append(state))
shutil.rmtree(TMP_LOG, ignore_errors=True)
status.LOG_DIR, status.LOG_ENABLED = TMP_LOG, True
secret = "这是一句不该出现在日志里的话"
status.set("在听", "请说话……")
time.sleep(0.3)
status.set("识别中", "")
status.set("说话中", secret)
status.set("待命中", "说“星期八”唤醒")
status.LOG_ENABLED = False
check("每一步都通知到了", heard[-4:] == ["在听", "识别中", "说话中", "待命中"], f"{heard[-4:]}")
log_text = "".join(open(os.path.join(TMP_LOG, f), encoding="utf-8").read() for f in os.listdir(TMP_LOG))
print("    日志内容：\n      " + "\n      ".join(log_text.strip().splitlines()))
check("日志记下了每一步用了几秒", "在听（用了 0.3" in log_text)
check("日志里没有你说的话、也没有助手的回答", secret not in log_text and "请说话" not in log_text)
shutil.rmtree(TMP_LOG, ignore_errors=True)

print("\n===== 3. 状态窗截图 =====")
mode = {"value": "always"}
ov = overlay.Overlay(mode=lambda: mode["value"]).start()
time.sleep(1.5)
shutil.rmtree(SHOTS, ignore_errors=True)
os.makedirs(SHOTS)
shots = [("待命中", "说“星期八”唤醒"), ("在听", "请说话……"), ("思考中", "大模型还在加载（唤醒后第一次要等几秒）"),
         ("执行", "设置音量"), ("说话中", "好的，音量已经调到百分之三十了。"), ("没听清", "请再说一遍")]
for i, (state, detail) in enumerate(shots, 1):
    status.set(state, detail)
    for _ in range(12):  # 让音量条动起来
        status.level(0.03 + 0.03 * (i % 2))
        time.sleep(0.1)
    if state != "待命中":
        time.sleep(1.0)  # 让“用时”数字跑起来
    img = ImageGrab.grab(bbox=ov.bbox, all_screens=True)
    path = os.path.join(SHOTS, f"{i}-{state}.png")
    img.save(path)
    colors = len(img.convert("RGB").getcolors(maxcolors=1_000_000))
    check(f"“{state}”画出来了", colors > 20, f"{img.size[0]}x{img.size[1]}，{colors} 种颜色 → {os.path.basename(path)}")

print("\n===== 4. 自动隐藏 =====")
mode["value"] = "auto"
status.set("待命中", "说“星期八”唤醒")
time.sleep(1.0)
check("回到待命 1 秒时还显示着", ov.visible)
time.sleep(3.0)
check("回到待命 3 秒后自动隐藏", not ov.visible)
status.set("在听", "请说话……")
time.sleep(0.5)
check("唤醒后马上又显示出来", ov.visible)
mode["value"] = "off"
time.sleep(0.5)
check("设成“不显示”就隐藏", not ov.visible)
ov.stop()
time.sleep(0.5)

print(f"\n结果：{sum(results)}/{len(results)} 项通过")
