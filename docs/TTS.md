# TTS 接口说明

PuPu 现在只保留了一个**通用 TTS 接口层**，项目里**不再内置 GPT-SoVITS**。

这意味着：

- 文字回复逻辑不受影响
- `/voice on`、`/voice off`、`/voice status` 这些开关还保留
- 如果没有额外安装 TTS provider，PuPu 会自动退回成“只发文字”

## 当前设计

TTS 入口在：

- [pupu/tts.py](/D:/ProjectICreate/PuPuBot/pupu_20260425_105129/pupu/tts.py)

它现在负责三件事：

1. 读取通用 TTS 配置
2. 维护 provider 注册表
3. 把合成结果落成音频文件，供 OneBot/NapCat 发送语音

项目默认**没有任何已安装 provider**。

## 通用环境变量

```env
PUPU_TTS_ENABLED=false
PUPU_TTS_REPLY_DEFAULT=false
PUPU_TTS_PROVIDER=
PUPU_TTS_BASE_URL=
PUPU_TTS_VOICE=
PUPU_TTS_MAX_CHARS=120
PUPU_TTS_TIMEOUT=60
PUPU_TTS_AUDIO_FORMAT=wav
PUPU_TTS_NORMALIZE_AUDIO=false
PUPU_TTS_FFMPEG=
```

含义：

- `PUPU_TTS_ENABLED`: 是否启用 TTS 功能
- `PUPU_TTS_REPLY_DEFAULT`: 启动后是否默认追加语音回复
- `PUPU_TTS_PROVIDER`: 当前使用的 provider 名称
- `PUPU_TTS_BASE_URL`: 给 HTTP 类 provider 预留的通用地址
- `PUPU_TTS_VOICE`: 给 provider 预留的通用音色名
- `PUPU_TTS_MAX_CHARS`: 超过长度直接跳过语音
- `PUPU_TTS_TIMEOUT`: provider 调用超时秒数
- `PUPU_TTS_AUDIO_FORMAT`: 期望输出格式，默认 `wav`
- `PUPU_TTS_NORMALIZE_AUDIO`: 是否在输出 wav 后做一次归一化
- `PUPU_TTS_FFMPEG`: 可选的 ffmpeg 路径

## 以后怎么接新的 TTS

在 [pupu/tts.py](/D:/ProjectICreate/PuPuBot/pupu_20260425_105129/pupu/tts.py) 里注册一个 provider：

```python
from pupu.tts import register_tts_provider, TTSConfig

def my_tts_provider(text: str, config: TTSConfig):
    audio_bytes = b"..."
    return audio_bytes, "wav"

register_tts_provider("my_provider", my_tts_provider)
```

provider 允许返回：

- `bytes`
- `(bytes, "wav")` 这种二元组
- 已经写好的 `Path`
- `None`

然后把环境变量设成：

```env
PUPU_TTS_ENABLED=true
PUPU_TTS_PROVIDER=my_provider
```

## 降级行为

下面这些情况都会自动只发文字：

- `PUPU_TTS_ENABLED=false`
- `PUPU_TTS_PROVIDER` 为空
- 指定的 provider 还没接入项目
- 回复超过 `PUPU_TTS_MAX_CHARS`
- provider 调用失败
- provider 没有返回可用音频

## 验证

```bash
.\.venv\Scripts\python.exe -m unittest tests.test_tts
```
