"""
dummy_sleep.py —— 用来测试 housekeeping.close_old_instances() 的“替身程序”

    不带参数运行：什么也不做，只是待着 120 秒（模拟一个忘了关的 listen.py）
    带 --close-others 运行：像 listen.py 启动时那样，关掉其他正在运行的 dummy_sleep.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import housekeeping  # noqa: E402

if "--close-others" in sys.argv:
    closed = housekeeping.close_old_instances(__file__)
    print(f"关掉了 {closed} 个旧的 dummy_sleep.py；我自己还在运行：PID {os.getpid()}")
else:
    time.sleep(120)
