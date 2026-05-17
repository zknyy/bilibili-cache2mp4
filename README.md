# bilibili-cache2mp4

将 B 站（bilibili.com）缓存到本地的 `.m4s` 文件处理并转换为可播放的 `mp4` 文件。

## 目录结构

- `run-dir.py`
  - 在指定目录中查找两个 `.m4s` 文件（不包含 `audio.m4s` 和 `video.m4s`）
  - 按文件名排序后，分别删除每个文件的前 9 个字节
  - 将处理后的文件保存为 `audio.m4s` 和 `video.m4s`
- `run-dir-all.py`
  - 功能与 `run-dir.py` 类似
  - 处理后进一步调用 `ffmpeg` 将 `audio.m4s` 和 `video.m4s` 合并为 `output.mp4`
  - 最终将 `output.mp4` 移动到脚本运行目录

## 要求

- Python 3
- `ffmpeg` 可执行文件已安装并添加到系统 `PATH`（仅 `run-dir-all.py` 需要）

## 使用方法

### 1. 生成 `audio.m4s` 和 `video.m4s`

```powershell
python run-dir.py <目录路径>
```

如果不指定目录，则默认处理当前目录：

```powershell
python run-dir.py
```

结果：

- 生成 `audio.m4s`
- 生成 `video.m4s`

### 2. 生成 `output.mp4`

```powershell
python run-dir-all.py <目录路径>
```

如果不指定目录，则默认处理当前目录：

```powershell
python run-dir-all.py
```

结果：

- 生成 `output.mp4` 并移动到当前脚本执行目录

## 注意事项

- 目录中必须恰好包含 2 个原始 `.m4s` 文件（排除 `audio.m4s`/`video.m4s`）
- 两个原始 `.m4s` 文件会按文件名排序后分别处理，较小文件生成 `audio.m4s`，较大文件生成 `video.m4s`
- 如果 `ffmpeg` 未安装或未配置到 `PATH`，`run-dir-all.py` 将无法完成合并

## 错误提示

- `错误：在目录 ... 下找到 N 个 .m4s 文件，需要恰好 2 个。`
  - 说明目标目录中的 `.m4s` 文件数量不正确
- `错误：未找到 ffmpeg 命令，请确保 ffmpeg 已安装并加入 PATH。`
  - 说明 `ffmpeg` 不可用
