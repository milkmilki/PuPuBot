# GPT-SoVITS TTS 接入说明

PuPu 的语音回复是可选功能：文字回复始终优先发送，GPT-SoVITS 生成失败时自动降级为文字。

## 部署位置

GPT-SoVITS 独立部署在：

```text
D:\ProjectICreate\PuPuBot\GPT-SoVITS
```

参考声线文件放在：

```text
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.wav
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.txt
```

`pupu_ref.txt` 需要写入参考音频的准确台词。参考音频是日文时，配置 `PUPU_TTS_PROMPT_LANG=ja`；PuPu 生成中文时，配置 `PUPU_TTS_TEXT_LANG=zh`。

## 启动 GPT-SoVITS API

在 GPT-SoVITS 的 conda 环境中运行：

```powershell
python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

默认 API 地址：

```text
http://127.0.0.1:9880/tts
```

## PuPu 配置

`.env` 中的相关配置：

```env
PUPU_TTS_ENABLED=true
PUPU_TTS_REPLY_DEFAULT=false
PUPU_TTS_BASE_URL=http://127.0.0.1:9880
PUPU_TTS_REF_AUDIO=D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.wav
PUPU_TTS_PROMPT_TEXT=
PUPU_TTS_PROMPT_TEXT_FILE=D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.txt
PUPU_TTS_PROMPT_LANG=zh
PUPU_TTS_TEXT_LANG=zh
PUPU_TTS_MAX_CHARS=120
PUPU_TTS_TIMEOUT=60
PUPU_TTS_MEDIA_TYPE=wav
PUPU_TTS_TEXT_SPLIT_METHOD=cut5
PUPU_TTS_NORMALIZE_AUDIO=true
PUPU_TTS_FFMPEG=D:\ProjectICreate\PuPuBot\Miniforge3\envs\GPTSoVits\Library\bin\ffmpeg.exe
PUPU_TTS_TOP_K=15
PUPU_TTS_TOP_P=1.0
PUPU_TTS_TEMPERATURE=1.0
PUPU_TTS_REPETITION_PENALTY=1.35
PUPU_TTS_SPEED_FACTOR=1.0
PUPU_TTS_SEED=-1
PUPU_TTS_PARALLEL_INFER=true
PUPU_TTS_SAMPLE_STEPS=32
PUPU_TTS_SUPER_SAMPLING=false
```

修改 `.env` 后要重启 PuPu。

`PUPU_TTS_ENABLED` 表示 TTS 功能可用；`PUPU_TTS_REPLY_DEFAULT` 表示 PuPu 启动后是否默认追加语音。建议保持 `PUPU_TTS_ENABLED=true`、`PUPU_TTS_REPLY_DEFAULT=false`，再用命令临时开关语音回复：

```text
/voice on
/voice off
/voice status
```

也可以用 `/语音 on`、`/语音 off`。

## 行为

- OneBot/NapCat：先发送文字，再发送一条整段语音。
- QQ 官方适配器：保持文字，不发送语音。
- 回复超过 `PUPU_TTS_MAX_CHARS`：跳过语音，只发文字。
- 服务未启动、参考音频缺失、参考文本为空、请求失败：只发文字，并在日志里输出 `[pupu][tts]`。

## 验证

运行单元测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_tts
```

运行相关回归测试：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_tts tests.test_buffering tests.test_scheduler
```

生成的音频缓存位于：

```text
data\tts_cache
```

## 预计耗时

当前机器按 Windows CPU 推理规划。首次加载模型通常需要 30-120 秒；服务热启动后，短句约 4-12 秒，普通一两句话约 8-25 秒，超过 120 字可能 30 秒以上。

## 本机 Windows CPU 修正

官方脚本在这台机器上已完成安装。部署时为了绕开 Windows 源码编译问题，`D:\ProjectICreate\PuPuBot\GPT-SoVITS\requirements.txt` 做了这些本机修正：

- `pyopenjtalk>=0.4.1` 改为 `pyopenjtalk-plus==0.4.1.post7`，它提供同名 `pyopenjtalk` 模块和 Windows wheel。
- `jieba_fast` 改为普通 `jieba`，并把 `GPT_SoVITS/text/chinese.py`、`chinese2.py`、`tone_sandhi.py` 的导入切到 `jieba`。
- 删除 `--no-binary=opencc`，并固定 `opencc==1.2.0` 使用 Windows wheel。
- x64 CPU 路线使用 `onnxruntime`，不是 `onnxruntime-gpu`。

本机还添加了：

```text
D:\ProjectICreate\PuPuBot\启动GPT-SoVITS.bat
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\
```

把日文参考音频放成 `pupu_ref.wav`，把完全对应的日文台词写入 `pupu_ref.txt` 后，再启动 GPT-SoVITS API 和 PuPu。

当前本机参考文件已经准备好：

```text
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.wav
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref.txt
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\pupu_ref_full.txt
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\source_full.mp3
D:\ProjectICreate\PuPuBot\GPT-SoVITS\pupu_reference\source_full.srt
```

当前 `pupu_ref.wav` 使用 `Video Project 3.m4a` 的 2.496-6.044 秒片段，已转为 32kHz 单声道 wav；`pupu_ref.txt` 只保留对应台词：`一个来自福建的美籍华人，我的声音年轻好客`。因为参考音频是中文，`PUPU_TTS_PROMPT_LANG=zh`。

如果生成语音有明显电音感，优先检查三件事：

- 参考音频要用 3-10 秒内的干净单人声，尽量不要用压缩痕迹重、带混响、带音乐、音量很小的片段。
- `pupu_ref.txt` 必须是参考音频逐字对应的纯台词，不要保留 SRT 编号和时间轴。
- PuPu 侧默认开启 `PUPU_TTS_NORMALIZE_AUDIO=true`，会把 GPT-SoVITS 返回的 wav 做一次响度归一化，降低 QQ/NapCat 二次转码时的金属感。
- 当前采样参数已退回 GPT-SoVITS API 默认值：`TOP_K=15`、`TOP_P=1.0`、`TEMPERATURE=1.0`、`REPETITION_PENALTY=1.35`、`SPEED_FACTOR=1.0`、`PARALLEL_INFER=true`。
