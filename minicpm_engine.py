"""启动已经下载的 MiniCPM-o，并通过本机接口收发音频。"""

import json
import logging
import socket
import subprocess
import sys
import time
import wave
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np


ROOT = Path(__file__).resolve().parent / "models" / "minicpm-o-4.5"
MODEL = "models/MiniCPM-o-4_5-Q4_K_M.gguf"
BINARY = "portable/{app}/_internal/resources/build/bin/Release/llama-server.exe"
VOICE = "portable/{app}/_internal/resources/apps/assets/ref_audio/ref_minicpm_signature.wav"
PREFIX = "<|im_start|>system\n你是星期八。请用这段音频的音色回答用户。\n<|audio_start|>"
SUFFIX = "<|audio_end|>请用简短自然的中文回答用户的最新问题。<|im_end|>\n<|im_start|>user\n"
INPUT_RATE = 16000
OUTPUT_RATE = 24000
CONTEXT_SIZE = 4096
REPLY_TOKENS = 40
BREAK_SETTLE = 0.3  # 打断后最多等这么久确认 T2W 已清理，否则重置会话
REARM_GAP = 0.03  # 旧轮最后一块写完后，T2W 回到循环顶部处理打断所需的余量


def pcm16(samples):
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


class Engine:
    def __init__(self, folder):
        self.folder = folder
        self.input_path = folder / "input.wav"
        self.proc = None
        self.http = build_opener(ProxyHandler({}))
        self.round = None
        self.broken = None

    def start(self):
        if sys.platform == "darwin":
            from local_models import MINICPM_MAC_MESSAGE
            raise RuntimeError(f"MiniCPM-o {MINICPM_MAC_MESSAGE}")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.address = f"http://127.0.0.1:{port}"
        command = [
            str(ROOT / BINARY), "--model", MODEL, "--host", "127.0.0.1",
            "--port", str(port), "--n-gpu-layers", "99", "--ctx-size", str(CONTEXT_SIZE),
            "--parallel", "1", "--no-webui", "--temp", "0.5", "--seed", "42",
        ]
        self.proc = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 35
        while self.proc.poll() is None:
            try:
                self.request("/health", timeout=1)
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("本地模型超过 35 秒未启动")
                time.sleep(0.15)
        else:
            raise RuntimeError(f"本地模型启动时退出（{self.proc.returncode}）")

        # 服务生成时会锁住打断接口，限制每次生成长度，给短插话留出响应时间。
        self.request("/v1/stream/omni_init", {
            "media_type": 1, "duplex_mode": False, "use_tts": True, "n_predict": REPLY_TOKENS,
            "model_dir": "models", "tts_bin_dir": "models/tts",
            "tts_gpu_layers": 99, "token2wav_device": "gpu:0",
            "voice_clone_prompt": PREFIX, "assistant_prompt": SUFFIX,
            "output_dir": self.folder.relative_to(ROOT).as_posix(),
        }, timeout=40)
        self.reset_session()

    def reset_session(self):
        # 更新滑窗时服务会清空上下文，所以在同一次请求里重新送入系统提示的音色。
        self.request("/v1/stream/update_session_config", {
            "voice_audio": VOICE, "sliding_window_mode": "basic",
            "sliding_window_high_water": 3200, "sliding_window_low_water": 2600,
        })

    def request(self, path, payload=None, timeout=12):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(self.address + path, data, {"Content-Type": "application/json"})
        with self.http.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8")

    def recover(self, *, next_round=None, on_zombie=None):
        """打断后让下一轮 prefill(cnt=0) 不再白等服务端写死的 5 秒（omni.cpp:8795）。

        /break 要等 decode 释放锁才返回，返回时 LLM 已退出，但 T2W 可能还在合成最后一块。
        T2W 写完这块回到循环顶部，清队列并清掉 break_event（omni.cpp:8470-8494），却不置
        speek_done，于是 prefill(0) 等满 5 秒。break 返回后旧轮目录又出现新 WAV，
        就说明 T2W 已处理完打断；这时再发一次 break，prefill(0) 会走服务端自己的
        “break_event 已触发”分支（omni.cpp:8786），置 speek_done、清 TTS 队列并保留上下文。
        """
        broken, self.broken = self.broken, None
        if broken is None:
            return "normal", 0.0
        acked, round_index, before, seen_wavs = broken
        start = time.monotonic()
        next_folder = self.wave_folder(next_round) if next_round is not None else None

        def log_zombies():
            if on_zombie is None or next_folder is None or seen_wavs is None:
                return
            found = self._wave_snapshot()
            if found is None:
                return
            for path in found - seen_wavs:
                if path.parent != next_folder:
                    on_zombie(round_index, path.relative_to(self.folder).as_posix())
            seen_wavs.update(found)

        try:
            while True:
                log_zombies()
                fresh = [path for path in self.wave_folder(round_index).glob("wav_*.wav") if path.name not in before]
                if fresh:
                    written = max(path.stat().st_mtime for path in fresh)
                    if time.time() - written >= REARM_GAP:
                        # 还没有任何一轮正常说完时 warmup_done=false，prefill(0) 不等待也不清 break_event
                        # （omni.cpp:8784、6583），补发的 break 会让新 decode 直接退出，所以不补发。
                        if not any(self.folder.glob("round_*/tts_wav/generation_done.flag")):
                            log_zombies()
                            return "cnt0_cold", round((time.monotonic() - start) * 1000, 1)
                        self.request("/v1/stream/break", {"reason": "rearm_for_prefill"})
                        log_zombies()
                        return "cnt0", round((time.monotonic() - start) * 1000, 1)
                elif time.monotonic() - acked >= BREAK_SETTLE:
                    # 没看到清理证据，重置会话，避免残留打断标志让新回复无声或白等 5 秒。
                    self.reset_session()
                    log_zombies()
                    return "reset", round((time.monotonic() - start) * 1000, 1)
                time.sleep(0.01)
        except BaseException:
            # 补发 break 或重置超时时，仍补扫一次请求期间落盘的 WAV；补扫失败不覆盖原异常。
            try:
                log_zombies()
            except Exception:
                pass
            raise

    def prefill(self, samples, *, next_round=None, on_zombie=None):
        branch, wait_ms = self.recover(next_round=next_round, on_zombie=on_zombie)
        with wave.open(str(self.input_path), "wb") as writer:
            writer.setparams((1, 2, INPUT_RATE, 0, "NONE", "not compressed"))
            writer.writeframes(pcm16(samples))
        start = time.monotonic()
        self.request("/v1/stream/prefill", {
            "audio_path_prefix": self.input_path.relative_to(ROOT).as_posix(),
            "cnt": 0,
        })
        return branch, wait_ms, round((time.monotonic() - start) * 1000, 1)

    def decode(self, round_index):
        self.round = round_index
        response = self.request("/v1/stream/decode", {
            "stream": True, "length_penalty": 1.05, "round_idx": round_index,
        })
        # 解码流的 content 是助手生成的文字，音频输入没有对应的转写事件。
        fragments = []
        for line in response.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                logging.getLogger(__name__).warning("MiniCPM-o 解码文字格式无效")
                continue
            if not isinstance(event, dict):
                continue
            if "error" in event:
                raise RuntimeError("本地模型生成语音失败")
            content = event.get("content")
            if isinstance(content, str):
                fragments.append(content)
        return "".join(fragments).strip()

    def interrupt(self):
        self.request("/v1/stream/break", {"reason": "user_interrupt"})
        acked = time.monotonic()
        # 返回前写出的 WAV 属于打断生效前，返回后再出现的才证明 T2W 处理过打断。
        before = {path.name for path in self.wave_folder(self.round).glob("wav_*.wav")}
        # 根目录快照包含服务端自行创建的轮次，供恢复期间的日志使用。
        self.broken = (acked, self.round, before, self._wave_snapshot())
        # 返回 /break 的响应时刻，打点不把后面的目录扫描算进去。
        return acked

    def _wave_snapshot(self):
        try:
            return set(self.folder.glob("round_*/tts_wav/wav_*.wav"))
        except OSError:
            return None

    def wave_folder(self, round_index):
        return self.folder / f"round_{round_index:03d}" / "tts_wav"

    def output_done(self, round_index, offset=None):
        flag = self.wave_folder(round_index) / "generation_done.flag"
        if offset is None:
            return flag.is_file()
        # 标记里有末块编号，防止标记先于 WAV 轮询被看见时提前结束记录。
        try:
            last = int(flag.read_text(encoding="ascii"))
        except (OSError, ValueError):
            return False
        return round_index * 1000 + offset > last

    def output(self, round_index, offset):
        # 服务按 round_idx 分目录，每个目录从 round_idx * 1000 开始编号。
        folder = self.wave_folder(round_index)
        while True:
            path = folder / f"wav_{round_index * 1000 + offset}.wav"
            try:
                with wave.open(str(path), "rb") as reader:
                    if (reader.getframerate(), reader.getsampwidth(), reader.getnchannels()) != (OUTPUT_RATE, 2, 1):
                        return
                    frames = reader.getnframes()
                    raw = reader.readframes(frames)
                if not raw or len(raw) != frames * 2:
                    return  # 服务还没写完，下次轮询再读
            except (OSError, EOFError, wave.Error):
                return
            yield np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
            offset += 1

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=5)  # 进程退出后，CUDA 上下文才会释放
