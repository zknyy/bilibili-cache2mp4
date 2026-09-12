# bilibili-cache2mp4

将 B 站（bilibili.com）本地缓存的 `.m4s` 视频/音频片段转换为可播放的 `mp4` 文件。 有以下特点：
- 操作简单：一行命令搞定所有转换
- 速度快：多线程加速，转换非常快
- 绿色环保：无需安装，开源免费
- 只需一个文件：`run-dir-all.py`
- 其实还需要一个文件：[ffmpeg](https://ffmpeg.org/)
- 多平台/系统兼容：在Windows，Ubuntu，MacOS上直接运行，下载可执行文件：[Releases](https://github.com/zknyy/bilibili-cache2mp4/releases)

## 文件说明

- `run-dir.py`
  - 在指定目录中查找两个原始 `.m4s` 文件（排除已经生成的 `audio.m4s` 和 `video.m4s`）
  - 读取每个 `.m4s` 的 MP4 box 结构，据此判断它是视频流还是音频流（不依赖文件名）
  - 分别删除每个文件的前 9 个字节
  - 将视频流保存为 `video.m4s`、音频流保存为 `audio.m4s`（同目录下）
- `run-dir-all.py`
  - 处理逻辑与 `run-dir.py` 类似，先生成 `audio.m4s` 和 `video.m4s`
  - 再调用 [ffmpeg](https://ffmpeg.org/) 将音视频合并为 `output.mp4`
  - 如果当前目录下存在 `videoInfo.json`，会根据其中的 `tabName` 和 `uname` 自动生成最终文件名
  - 同一系列的多个视频（`groupId` 相同）会归入「系列名称-up主名称」子目录，详见下文
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
- 多目录场景下会自动识别同系列视频并归入子目录，见「系列视频（合集 / 多 P）的处理」

## 自动命名规则

- `run-dir-all.py` 会查找当前目录或指定目录中的 `videoInfo.json`
- 如果存在且包含 `tabName` 和 `uname`，最终输出文件名格式为：
  - `tabName by uname.mp4`
- 如果 `videoInfo.json` 不可用，则使用目标目录名作为文件名基础
- 如果输出文件名已存在，会自动添加 `_1`, `_2` 等后缀避免覆盖

## 系列视频（合集 / 多 P）的处理

当一次处理的多个缓存目录属于同一系列时（`videoInfo.json` 中 `groupId` 相同），
这些视频会被收进同一个子目录，便于整季归档：

- 子目录名：`系列名称-up主名称`（`groupTitle` + `-` + `uname`）
- 子目录内的文件名：只用视频名称（`tabName`），不再追加 up 主名称
- 系列内只有 1 个视频、或该视频没有 `groupId` / `groupTitle` 时，不建子目录，
  沿用上面的平铺命名规则
- 单独处理一个目录（只传一个路径参数）时无法判断系列归属，同样维持平铺命名

示例：

```
python run-dir-all.py 41785167008 41788509107
```

这两个目录同属一个系列，处理结果是：

```
【B站精选】目前B站最全最细的Agent开发教程...-AI大模型-开发/
├── 00.【课前篇】做Agent开发，到底要学哪些技术栈？成品(2).mp4
└── 01.【课前篇】今年这么多人想转行做AI Agent工程师？真有那么好干吗？.mp4
```

处理前会先打印一份输出规划，列出检测到的系列、子目录名与其中的文件名。
系列目录已存在时会直接复用（不会新建 `_1` 目录），目录内的重名文件仍按
`_1`、`_2` 规则避让。

## 注意事项

- 目标目录中必须恰好包含 2 个原始 `.m4s` 文件（排除 `audio.m4s` 和 `video.m4s`）
- 脚本会读取文件内容判断流类型，因此 `audio.m4s` 一定是音频流、`video.m4s` 一定是视频流，与文件名无关
- B 站缓存的文件名形如 `{cid}-{分P}-{流编号}.m4s`。流编号与音视频的对应关系随清晰度和编码变化（例如 `30080` 是视频、`30280` 是音频），**不能按文件名排序推断**，脚本也不会这样做
- `run-dir-all.py` 依赖 [ffmpeg](https://ffmpeg.org/)，若未安装或未添加到 `PATH`，会提示错误并退出
- `videoInfo.json` 中的标题会过滤非法文件名字符，确保生成的 MP4 名称可在 Windows / Linux / macOS 中使用

## 常见错误

- `错误：在目录 ... 下找到 N 个 .m4s 文件，需要恰好 2 个。`
  - 目标目录中 `.m4s` 文件数量不正确，可能存在多余或缺少片段
- `错误：目录 '...' 不存在或不是有效目录。`
  - 指定的路径不是目录或目录不存在
- `ffmpeg` 执行失败
  - 请确认已从 [ffmpeg 官网](https://ffmpeg.org/) 下载并正确安装 `ffmpeg`，且已加入 `PATH`
