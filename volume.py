"""
volume.py —— 控制系统音量

提供 5 个函数（第二步会把它们作为“工具”交给大模型调用）：
    get_volume()          查看当前音量和是否静音
    set_volume(percent)   把音量设到指定百分比（0~100）
    change_volume(delta)  调大或调小：delta 是正数就调大，负数就调小
    mute()                静音
    unmute()              取消静音

每个函数都返回一个字典，例如：
    {"volume": 40, "muted": False, "message": "音量已设为 40%"}
其中 message 是给人看的中文说明，之后也会把它告诉大模型。

单独运行这个文件可以查看当前音量：
    .venv\\Scripts\\python.exe volume.py
"""

import subprocess
import sys

if sys.platform != "darwin":
    from pycaw.pycaw import AudioUtilities


def _endpoint():
    """拿到“默认扬声器”的音量控制接口（内部使用）。"""
    # 每次都重新获取，这样即使默认播放设备变了，也能控制到正确的设备
    return AudioUtilities.GetSpeakers().EndpointVolume


def _clamp(percent):
    """把数字限制在 0~100 之间并取整。也接受 "50" 这样的文字数字。"""
    return max(0, min(100, int(round(float(percent)))))


def _osascript(*commands):
    args = ["osascript"]
    for command in commands:
        args.extend(("-e", command))
    return subprocess.run(args, capture_output=True, text=True, check=True, timeout=5).stdout.strip()


def _set_mute(muted):
    if sys.platform == "darwin":
        _osascript("set volume with output muted" if muted else "set volume without output muted")
    else:
        _endpoint().SetMute(int(muted), None)


def _status(message):
    """读取当前音量和静音状态，连同说明文字一起返回（内部使用）。"""
    if sys.platform == "darwin":
        output = _osascript("set levels to (get volume settings)",
                            "return {(output volume of levels), (output muted of levels)}")
        level, _, mute_text = output.partition(",")
        percent, muted = _clamp(level), mute_text.strip() == "true"
    else:
        ep = _endpoint()
        # Windows 内部用 0.0~1.0 表示音量。
        percent = _clamp(ep.GetMasterVolumeLevelScalar() * 100)
        muted = bool(ep.GetMute())
    return {"volume": percent, "muted": muted, "message": message}


def get_volume():
    """查看当前音量。"""
    status = _status("")
    mute_text = "（静音中）" if status["muted"] else ""
    status["message"] = f"当前音量 {status['volume']}%{mute_text}"
    return status


def set_volume(percent):
    """把音量设到指定百分比（0~100），超出范围会自动限制。"""
    target = _clamp(percent)
    # 记下改之前的音量，写进结果里，这样你说“调回去”时，模型知道该回到多少
    if sys.platform == "darwin":
        old = _status("")
        before, was_muted = old["volume"], old["muted"]
        _osascript(f"set volume output volume {target}", "set volume without output muted")
    else:
        ep = _endpoint()
        before = _clamp(ep.GetMasterVolumeLevelScalar() * 100)
        was_muted = bool(ep.GetMute())
        ep.SetMasterVolumeLevelScalar(target / 100, None)
        # 和 Windows 自带的音量滑块一样：调音量时顺便取消静音
        ep.SetMute(0, None)
    unmuted = "，并取消了静音" if was_muted else ""
    return _status(f"音量已从 {before}% 设为 {target}%{unmuted}")


def change_volume(delta):
    """在当前音量基础上调大（正数）或调小（负数）。"""
    before = get_volume()["volume"]
    result = set_volume(before + float(delta))
    action = "调大" if float(delta) > 0 else "调小"
    result["message"] = f"音量已从 {before}% {action}到 {result['volume']}%"
    return result


def mute():
    """静音（不改变音量大小）。"""
    _set_mute(True)
    return _status("已静音")


def unmute():
    """取消静音。"""
    _set_mute(False)
    status = _status("")
    status["message"] = f"已取消静音，当前音量 {status['volume']}%"
    return status


if __name__ == "__main__":
    print(get_volume()["message"])
