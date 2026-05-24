# bilibili-cache2mp4

将 B 站（bilibili.com）本地缓存的 `.m4s` 视频/音频片段转换为可播放的 `mp4` 文件。 有以下特点：
- 操作简单：一行命令搞定所有转换
- 速度快：多线程加速，转换非常快
- 绿色环保：无需安装，开源免费
- 只需一个文件：`run-dir-all.py`
- 其实还需要一个文件：[ffmpeg](https://ffmpeg.org/)
- Python 3，多平台/系统兼容：在Windows，Linux，MacOS的Python环境中运行

## 文件说明

- `run-dir.py`
  - 在指定目录中查找两个原始 `.m4s` 文件（排除已经生成的 `audio.m4s` 和 `video.m4s`）
  - 按文件名排序后分别删除每个文件的前 9 个字节
  - 将处理后的文件保存为同目录下的 `audio.m4s` 和 `video.m4s`
- `run-dir-all.py`
  - 处理逻辑与 `run-dir.py` 类似，先生成 `audio.m4s` 和 `video.m4s`
  - 再调用 [ffmpeg](https://ffmpeg.org/) 将音视频合并为 `output.mp4`
  - 如果当前目录下存在 `videoInfo.json`，会根据其中的 `tabName` 和 `uname` 自动生成最终文件名
  - 成功生成 MP4 后会删除临时 `audio.m4s` 和 `video.m4s`
  - 支持：单目录同步处理、多个目录并行处理、以及无参数自动扫描当前目录下的纯数字子目录

## 依赖要求

- Python 3
- [ffmpeg](https://ffmpeg.org/) 已安装并添加到系统 `PATH`（仅 `run-dir-all.py` 需要）

## 使用方法

### 1. 生成 `audio.m4s` 和 `video.m4s`

```powershell
python run-dir.py <目录路径>
```

如果不指定目录，则默认处理当前目录：

```powershell
python run-dir.py
```

处理完成后，目标目录中将生成：

- `audio.m4s`
- `video.m4s`

### 2. 生成 `output.mp4`

```powershell
python run-dir-all.py <目录路径>
```

如果不指定目录，则默认处理当前目录：

```powershell
python run-dir-all.py
```

`run-dir-all.py` 会产生：

- 先生成 `audio.m4s` 和 `video.m4s`
- 再生成 `output.mp4`
- 最终将 `output.mp4` 移动到脚本运行目录
- 成功后删除临时 `audio.m4s` 和 `video.m4s`

### 3. 多目录处理

```powershell
python run-dir-all.py 12345 67890
```

- 多个目录参数时，会并行处理这些目录
- 单个目录参数时，保持同步处理行为
- 不传参数时，会自动扫描当前目录下所有纯数字命名的子目录，并询问是否继续处理

## 自动命名规则

- `run-dir-all.py` 会查找当前目录或指定目录中的 `videoInfo.json`
- 如果存在且包含 `tabName` 和 `uname`，最终输出文件名格式为：
  - `tabName by uname.mp4`
- 如果 `videoInfo.json` 不可用，则使用目标目录名作为文件名基础
- 如果输出文件名已存在，会自动添加 `_1`, `_2` 等后缀避免覆盖

## 注意事项

- 目标目录中必须恰好包含 2 个原始 `.m4s` 文件（排除 `audio.m4s` 和 `video.m4s`）
- 脚本会按文件名排序后处理，较小名称的文件生成 `audio.m4s`，较大名称的文件生成 `video.m4s`
- `run-dir-all.py` 依赖 [ffmpeg](https://ffmpeg.org/)，若未安装或未添加到 `PATH`，会提示错误并退出
- `videoInfo.json` 中的标题会过滤非法文件名字符，确保生成的 MP4 名称可在 Windows / Linux / macOS 中使用

## 常见错误

- `错误：在目录 ... 下找到 N 个 .m4s 文件，需要恰好 2 个。`
  - 目标目录中 `.m4s` 文件数量不正确，可能存在多余或缺少片段
- `错误：目录 '...' 不存在或不是有效目录。`
  - 指定的路径不是目录或目录不存在
- `ffmpeg` 执行失败
  - 请确认已从 [ffmpeg 官网](https://ffmpeg.org/) 下载并正确安装 `ffmpeg`，且已加入 `PATH`
