"""
appicon.py —— 画助手的图标（托盘图标、快捷方式图标都用它）

    一个深色圆，外圈是一道彩色光环，中间一个“八”字。光环颜色表示状态：
        青色 = 待命中    橙色 = 对话中    灰色 = 已暂停 / 正在退出
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

COLORS = {
    "启动中": (90, 160, 200),
    "待命中": (0, 210, 255),
    "对话中": (255, 170, 0),
    "已暂停": (140, 140, 140),
    "正在退出": (140, 140, 140),
}

FONTS = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]
MAC_FONTS = ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
             "/System/Library/Fonts/Supplemental/Songti.ttc"]


def make_image(color=COLORS["待命中"], size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = max(1, size // 16)
    draw.ellipse([margin, margin, size - margin, size - margin],
                 fill=(14, 22, 40, 255), outline=color, width=max(2, size // 10))
    font = None
    for path in MAC_FONTS if sys.platform == "darwin" else FONTS:
        if os.path.exists(path):
            font = ImageFont.truetype(path, int(size * 0.5))
            break
    if font is None and sys.platform == "darwin":
        # 系统中文字体不存在时，直接画出「八」的两笔。
        width = max(1, size // 13)
        draw.line([(size * .43, size * .34), (size * .39, size * .52), (size * .29, size * .66)], color, width)
        draw.line([(size * .55, size * .35), (size * .60, size * .53), (size * .71, size * .66)], color, width)
    else:
        draw.text((size / 2, size / 2 + size * 0.02), "八", fill=color,
                  font=font or ImageFont.load_default(), anchor="mm")
    return img


def save_ico(path):
    """保存成 .ico 文件（快捷方式用），包含从 16 到 256 像素的各种尺寸。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    make_image(size=256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                           (128, 128), (256, 256)])
