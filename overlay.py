"""
overlay.py —— 右下角的状态窗：用动画和文字实时显示助手在干嘛

    左边的动画：待命=呼吸光圈  在听=跟着你声音跳的音量条  识别/思考/执行=转圈  说话=扩散的声波
    右边的文字：现在在干嘛 + 这一步用了几秒（数字一直在涨，说明它在干活，不是卡住了）
                说话时会显示它说的那句话（像字幕）

    鼠标点它会直接点到下面的东西（不挡游戏、不抢焦点）。
    显示方式（托盘菜单里可以改）：
        auto   自动：唤醒后显示，回到待命 3 秒后自动隐藏（默认）
        always 一直显示
        off    不显示

单独运行可以看演示：.venv\\Scripts\\python.exe overlay.py
"""

import ctypes
import ctypes.wintypes
import math
import queue
import random
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import status

IDLE_STATES = {"待命中", "已暂停", "启动中", "群聊语音输入"}
HIDE_AFTER = 3.0                 # 自动模式下，回到待命几秒后隐藏
KEY = "#010203"                  # 透明色（用来做圆角）
BG = "#0E1628"                   # 窗口底色
FONT = "Microsoft YaHei UI"


def _hex(rgb, k=1.0):
    """颜色乘以亮度 k，混合到底色上。"""
    bg = (14, 22, 40)
    r, g, b = (int(bg[i] + (rgb[i] - bg[i]) * max(0.0, min(1.0, k))) for i in range(3))
    return f"#{r:02x}{g:02x}{b:02x}"


class Overlay:
    def __init__(self, mode=lambda: "auto"):
        self.mode = mode             # 返回 "auto"/"always"/"off" 的函数（托盘里可以随时改）
        self._queue = queue.Queue()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        status.subscribe(lambda state, detail: self._queue.put((state, detail)))
        return self

    def stop(self):
        """关掉状态窗，等它在自己的线程里收拾干净（否则退出程序时会报错）。"""
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------

    def _run(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 高分屏下不模糊
        except Exception:
            pass
        root = self.root = tk.Tk()
        s = self.scale = root.winfo_fpixels("1i") / 96
        self.w, self.h = int(330 * s), int(76 * s)
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.98)  # 几乎不透明，文字看得清
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        self.canvas = tk.Canvas(root, width=self.w, height=self.h, bg=KEY, highlightthickness=0)
        self.canvas.pack()
        self.title_font = tkfont.Font(family=FONT, size=12, weight="bold")
        self.detail_font = tkfont.Font(family=FONT, size=9)
        self._place()
        root.update_idletasks()
        self._click_through()

        st = status.current()
        self.state, self.detail, self.since = st["state"], st["detail"], st["since"]
        self.idle_since = time.time()
        self.visible = True
        self.smooth_level = 0.0
        self._tick()
        root.mainloop()
        # 窗口关掉后，在同一个线程里把界面相关的东西清理掉（Tk 要求只能由创建它的线程清理）
        self.title_font = self.detail_font = self.canvas = self.root = root = None
        import gc
        gc.collect()

    def _place(self):
        """放在屏幕右下角、任务栏上方。"""
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # 可用区域（不含任务栏）
        margin = int(16 * self.scale)
        x, y = rect.right - self.w - margin, rect.bottom - self.h - margin
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.bbox = (x, y, x + self.w, y + self.h)  # 窗口在屏幕上的位置（测试截图用）

    def _click_through(self):
        """让鼠标点击穿过窗口、不抢焦点、不出现在任务栏和 Alt+Tab 里。"""
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ex |= 0x80000 | 0x20 | 0x08000000 | 0x80  # LAYERED | TRANSPARENT | NOACTIVATE | TOOLWINDOW
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex)
        except Exception:
            pass

    # ------------------------------------------------------------------

    def _tick(self):
        """每秒约 30 次：处理新状态、决定显示/隐藏、画一帧。"""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                self.root.destroy()
                return
            self.state, self.detail = item
            self.since = time.time()
            if self.state in IDLE_STATES:
                self.idle_since = time.time()

        mode = self.mode()
        idle = self.state in IDLE_STATES
        show = mode == "always" or (mode == "auto" and (not idle or time.time() - self.idle_since < HIDE_AFTER))
        if show and not self.visible:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
            self.visible = True
        elif not show and self.visible:
            self.root.withdraw()
            self.visible = False
        if self.visible:
            self._draw()
        self.root.after(33, self._tick)

    def _draw(self):
        c, s = self.canvas, self.scale
        c.delete("all")
        w, h, r = self.w, self.h, int(18 * s)
        # 圆角背景
        for x0, y0, x1, y1 in ((0, 0, 2 * r, 2 * r), (w - 2 * r, 0, w, 2 * r),
                               (0, h - 2 * r, 2 * r, h), (w - 2 * r, h - 2 * r, w, h)):
            c.create_oval(x0, y0, x1, y1, fill=BG, outline=BG)
        c.create_rectangle(r, 0, w - r, h, fill=BG, outline=BG)
        c.create_rectangle(0, r, w, h - r, fill=BG, outline=BG)

        color, anim = status.style(self.state)
        t = time.time()
        cx, cy, R = h / 2, h / 2, h * 0.30
        if anim == "breathe":
            k = 0.55 + 0.45 * math.sin(t * 2.2)
            c.create_oval(cx - R, cy - R, cx + R, cy + R, outline=_hex(color, k), width=max(2, int(4 * s)))
        elif anim == "bars":
            self.smooth_level = 0.7 * self.smooth_level + 0.3 * min(1.0, status.current()["level"] * 12)
            for i in range(5):
                bh = R * (0.25 + 1.5 * self.smooth_level * (0.6 + 0.4 * math.sin(t * 9 + i * 1.3))
                          + 0.08 * random.random())
                x = cx - R + i * (2 * R / 4)
                c.create_line(x, cy - bh / 2, x, cy + bh / 2, fill=_hex(color), width=max(3, int(5 * s)),
                              capstyle="round")
        elif anim == "spin":
            c.create_oval(cx - R, cy - R, cx + R, cy + R, outline=_hex(color, 0.25), width=max(2, int(4 * s)))
            c.create_arc(cx - R, cy - R, cx + R, cy + R, start=-(t * 360) % 360, extent=100,
                         style="arc", outline=_hex(color), width=max(2, int(4 * s)))
        elif anim == "wave":
            for i in range(3):
                p = (t * 0.9 + i / 3) % 1.0
                rr = R * (0.4 + 0.8 * p)
                c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline=_hex(color, 1 - p), width=max(2, int(3 * s)))
        else:  # still
            c.create_oval(cx - R, cy - R, cx + R, cy + R, outline=_hex(color), width=max(2, int(4 * s)))

        # 文字
        x = h + int(4 * s)
        elapsed = t - self.since
        title = self.state if self.state in IDLE_STATES or elapsed < 0.8 else f"{self.state} · {elapsed:.1f} 秒"
        c.create_text(x, h * 0.36, text=title, anchor="w", fill="#FFFFFF", font=self.title_font)
        detail = self._fit(self.detail, w - x - int(14 * s))
        c.create_text(x, h * 0.70, text=detail, anchor="w", fill="#9FB0C8", font=self.detail_font)

    def _fit(self, text, width):
        """文字太长就截断，结尾加“…”。"""
        text = str(text or "")
        if self.detail_font.measure(text) <= width:
            return text
        while text and self.detail_font.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"


if __name__ == "__main__":
    # 演示：把每种状态轮流放一遍
    Overlay(mode=lambda: "always").start()
    demo = [("待命中", "说“星期八”唤醒"), ("确认唤醒词", "听起来像“星期八”"), ("说话中", "我在。"),
            ("在听", "请说话……"), ("识别中", ""), ("思考中", "第一次唤醒要加载大模型，约 5 秒"),
            ("执行", "调音量"), ("组织语言", ""), ("合成语音", ""), ("说话中", "好的，音量已经调到百分之三十了。"),
            ("复盘中", "整理刚才听到的话"), ("待命中", "说“星期八”唤醒")]
    for state, detail in demo:
        status.set(state, detail)
        for _ in range(20):
            status.level(random.random() * 0.08)
            time.sleep(0.1)
