import os
import sys
import struct
import argparse
from pathlib import Path

# ---------- 音视频流识别 ----------
# B站缓存的文件名形如 {cid}-{分P}-{流编号}.m4s，但流编号与音视频的对应关系会随
# 清晰度和编码变化（实测 30080 是视频、30280 是音频），所以不能靠文件名排序来
# 推断哪个文件是音频。这里直接读取 MP4 的 box 结构，用 moov/trak/mdia 下 hdlr
# 的 handler_type 判断真实流类型：不依赖文件名，也不需要 ffmpeg。
BILIBILI_PREFIX_BYTES = 9          # B站缓存文件头部的垃圾字节数
CONTAINER_BOXES = frozenset({
    "moov", "trak", "mdia", "minf", "stbl", "edts", "mvex", "dinf", "udta",
})
MAX_BOX_DEPTH = 8


def _find_handler_type(fp, start, end, depth=0):
    """在 [start, end) 的 box 范围内递归查找 hdlr 的 handler_type。"""
    if depth > MAX_BOX_DEPTH:
        return None
    offset = start
    while offset + 8 <= end:
        fp.seek(offset)
        header = fp.read(8)
        if len(header) < 8:
            return None
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8].decode("latin-1", "replace")
        header_size = 8
        if size == 1:                      # 64 位长度
            extra = fp.read(8)
            if len(extra) < 8:
                return None
            size = struct.unpack(">Q", extra)[0]
            header_size = 16
        elif size == 0:                    # 一直延伸到文件末尾
            size = end - offset
        if size < header_size or offset + size > end:
            return None                    # 结构异常，放弃判定
        if box_type == "hdlr":
            payload = fp.read(min(12, size - header_size))
            if len(payload) >= 12:
                # hdlr 布局：version+flags(4) + pre_defined(4) + handler_type(4)
                return payload[8:12].decode("latin-1", "replace")
            return None
        if box_type in CONTAINER_BOXES:
            found = _find_handler_type(fp, offset + header_size, offset + size, depth + 1)
            if found:
                return found
        offset += size
    return None


def detect_media_kind(path):
    """判断 .m4s 是视频流还是音频流，返回 'video' / 'audio' / None（无法判定）。"""
    try:
        end = path.stat().st_size
        with open(path, "rb") as fp:
            # 0 = 已剥头的文件；9 = B站原始缓存文件
            for start in (0, BILIBILI_PREFIX_BYTES):
                if start >= end:
                    continue
                handler = _find_handler_type(fp, start, end)
                if handler == "vide":
                    return "video"
                if handler == "soun":
                    return "audio"
    except OSError:
        return None
    return None


def split_audio_video(files):
    """把两个 .m4s 分派成 (音频文件, 视频文件)。"""
    kinds = {f: detect_media_kind(f) for f in files}
    audio = [f for f in files if kinds[f] == "audio"]
    video = [f for f in files if kinds[f] == "video"]
    if len(audio) == 1 and len(video) == 1:
        return audio[0], video[0]
    # 内容判定失败时退回体积比较：B站缓存中视频流远大于音频流
    print("警告：无法从文件内容判定音视频流类型，改按体积区分（较大者视为视频）。")
    ordered = sorted(files, key=lambda f: (f.stat().st_size, f.name))
    return ordered[0], ordered[1]

def process_m4s_files(work_dir: Path):
    """在 work_dir 目录下处理两个 .m4s 文件"""
    # 获取工作目录下所有 .m4s 文件（排除已生成的目标文件）
    m4s_files = [f for f in work_dir.glob("*.m4s") 
                 if f.name not in ("audio.m4s", "video.m4s")]
    
    if len(m4s_files) != 2:
        print(f"错误：在目录 {work_dir} 下找到 {len(m4s_files)} 个 .m4s 文件，需要恰好 2 个。")
        sys.exit(1)
    
    # 按实际流类型分派音视频（不能用文件名排序推断）
    audio_src, video_src = split_audio_video(m4s_files)
    
    def strip_first_9_bytes(src: Path, dst: Path):
        """读取 src 文件，删除前 9 个字节后写入 dst 文件"""
        with open(src, 'rb') as fin:
            data = fin.read()
        if len(data) < BILIBILI_PREFIX_BYTES:
            print(f"警告：文件 {src.name} 大小不足 {BILIBILI_PREFIX_BYTES} 字节，删除后将变为空文件。")
            content = b''
        else:
            content = data[BILIBILI_PREFIX_BYTES:]
        with open(dst, 'wb') as fout:
            fout.write(content)
        print(f"已处理：{src.name} -> {dst.name} (删除前9字节)")
    
    # 目标文件也放在同一目录
    strip_first_9_bytes(audio_src, work_dir / "audio.m4s")
    strip_first_9_bytes(video_src, work_dir / "video.m4s")
    
    print(f"完成！处理目录：{work_dir}")

def main():
    parser = argparse.ArgumentParser(
        description="删除两个 .m4s 文件的前9字节，并按文件名大小分别保存为 audio.m4s 和 video.m4s"
    )
    parser.add_argument(
        "directory", 
        nargs="?", 
        default=".", 
        help="包含两个 .m4s 文件的目录路径（默认为当前目录）"
    )
    args = parser.parse_args()
    
    work_dir = Path(args.directory).resolve()
    if not work_dir.is_dir():
        print(f"错误：目录 '{work_dir}' 不存在或不是有效目录。")
        sys.exit(1)
    
    process_m4s_files(work_dir)

if __name__ == "__main__":
    main()