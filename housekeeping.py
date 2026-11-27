"""
housekeeping.py —— 两个“打扫卫生”的小工具，listen.py（以及第四步的主程序）会用到

    clear_screen()                 清空终端屏幕，连往上翻的历史记录一起清掉
    close_old_instances(脚本路径)   关掉其他正在运行同一个脚本的程序，释放显存
"""

import os
import sys

import psutil  # 查看和管理正在运行的程序（安装 pycaw 时已经一起装好了）


def clear_screen():
    """清空终端屏幕和往上翻的历史记录，之前显示的识别文字就看不到了。"""
    if not sys.stdout.isatty():
        return  # 输出没有显示在终端里（比如被保存到文件），就不用清
    if sys.platform == "win32":
        os.system("cls")  # 清掉当前屏幕
    # 有些终端（比如 Windows Terminal）用 cls 清不掉往上翻的历史，再发一个“清除历史”的控制码
    print("\x1b[3J\x1b[H\x1b[2J", end="", flush=True)


def _is_running_script(proc, target):
    """判断某个程序是不是在用 Python 运行 target 这个脚本。"""
    if not (proc.info["name"] or "").lower().startswith("python"):
        return False
    for arg in (proc.info["cmdline"] or [])[1:]:
        if not arg.lower().endswith(".py"):
            continue
        path = arg if os.path.isabs(arg) else os.path.join(proc.cwd(), arg)
        if os.path.normcase(os.path.abspath(path)) == target:
            return True
    return False


def find_instances(script_path):
    """找出其他正在运行 script_path 的进程（不包括自己和自己的“上级”）。"""
    target = os.path.normcase(os.path.abspath(script_path))

    # 自己和自己的“上级”（启动器、终端）不算
    me = psutil.Process()
    skip = {me.pid} | {p.pid for p in me.parents()}

    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if proc.pid in skip:
            continue
        try:
            if _is_running_script(proc, target):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # 程序刚好结束了，或者没有权限查看，跳过
    return found


def count_programs(procs):
    """统计“程序”个数：父进程也在名单里的，是同一个程序的另一半，不重复计数。"""
    pids = {p.pid for p in procs}
    count = 0
    for proc in procs:
        try:
            if proc.ppid() not in pids:
                count += 1
        except psutil.NoSuchProcess:
            pass
    return count


def close_old_instances(script_path):
    """
    关掉其他正在运行 script_path 的程序（不会关掉自己），返回关掉了几个。

    小知识：用 .venv\\Scripts\\python.exe 运行时，Windows 上其实会出现两个进程：
    一个“启动器”加一个真正干活的 Python。所以这里把两个都关掉，但只算作一个程序。
    """
    found = find_instances(script_path)
    programs = count_programs(found)

    for proc in found:
        try:
            proc.terminate()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(found, timeout=5)
    return programs
