"""
cuda_libs.py —— 让程序找到装在 .venv 里的 NVIDIA 显卡库（cuBLAS、cuDNN）

faster-whisper 用显卡时需要这些 .dll 文件。它们被 pip 装在
    .venv\\Lib\\site-packages\\nvidia\\<库名>\\bin
但 Windows 默认不会去那里找。这个文件只在“程序运行期间”告诉 Windows 去那里找，
不会修改系统的环境变量，程序结束后就失效了。

用法：在导入 faster_whisper 之前先导入它
    import cuda_libs  # noqa: F401
    from faster_whisper import WhisperModel
"""

import glob
import os
import sys


def _add_nvidia_dll_dirs():
    site_packages = os.path.join(sys.prefix, "Lib", "site-packages")
    bin_dirs = sorted(glob.glob(os.path.join(site_packages, "nvidia", "*", "bin")))
    for path in bin_dirs:
        os.add_dll_directory(path)
    # 有些库是“用到时才加载”的，这种情况 Windows 会按 PATH 查找，所以也临时加进 PATH
    os.environ["PATH"] = os.pathsep.join(bin_dirs + [os.environ.get("PATH", "")])
    return bin_dirs


NVIDIA_DLL_DIRS = _add_nvidia_dll_dirs() if sys.platform == "win32" else []

# RTX 50 系列显卡比较新，第一次运行时显卡驱动要把计算代码“现场编译”一遍（约 20 秒），
# 编译结果会缓存起来，之后就很快。默认缓存在 AppData\Roaming\NVIDIA\ComputeCache，
# 这里改成放在项目文件夹里，并把缓存上限调到 4GB，避免缓存被挤掉后又要重新编译。
if sys.platform == "win32":
    _PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
    os.environ["CUDA_CACHE_PATH"] = os.path.join(_PROJECT_DIR, ".cuda-cache")
    os.environ["CUDA_CACHE_MAXSIZE"] = str(4 * 1024 ** 3)
