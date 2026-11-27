# 模型放这里

本仓库不包含任何模型文件。请自己下载，解压后**文件夹名要和下面一致**（代码按这些名字找）。

| 文件夹名 | 作用 | 必需？ | 哪里下 |
|---|---|---|---|
| `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01` | 唤醒词“星期八”（第一道检测） | 必需 | [sherpa-onnx 发布页 kws-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models) |
| `sherpa-onnx-streaming-paraformer-bilingual-zh-en` | 边说边出字（流式识别） | 必需 | [sherpa-onnx 发布页 asr-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models) |
| `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 一句话说完后定稿识别、唤醒词二次确认 | 必需 | 同上 asr-models（SenseVoice int8） |
| `kokoro-multi-lang-v1_0` | 离线语音合成（没网时说话） | 推荐 | [sherpa-onnx 发布页 tts-models](https://github.com/k2-fsa/sherpa-onnx/releases/tag/tts-models) |
| `faster-whisper-large-v3-turbo` | 备用识别（Whisper，CTranslate2 格式） | 可选 | Hugging Face 上 large-v3-turbo 的 CTranslate2 / faster-whisper 版本 |
| `qwen3-asr-1.7b-int8` | 备用识别（千问 ASR） | 可选 | 见 `transcription.py` |
| `minicpm-o-4.5` | MiniCPM-o 4.5 全双工对话（只支持 Windows + NVIDIA） | 可选 | OpenBMB 的 MiniCPM-o 4.5，用 GGUF（Q4_K_M），目录结构见 `minicpm_engine.py` |

另外：
- `wakeword_keywords.txt` 不用下载，程序会根据唤醒词自动生成。
- 本地大脑（Gemma 等）不放在这里，由 Ollama 管理：`ollama pull gemma4:12b` 或 `ollama pull gemma4:e4b`。
- 每个模型有自己的许可证，请自行确认后再使用。
