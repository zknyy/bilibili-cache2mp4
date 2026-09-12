import os
import sys
import json
import re
import struct
import argparse
import subprocess
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing

# ---------- 文件名安全处理 ----------
def sanitize_filename(name: str) -> str:
    """
    移除 Windows / Linux / macOS 文件名中不允许的字符，替换为下划线。
    允许：字母、数字、中文、空格、点、括号、连字符等常见安全字符。
    """
    # 禁止的字符（Windows 和 Unix 常见）
    forbidden_chars = r'[<>:"/\\|?*]'
    # 控制字符去除
    name = re.sub(forbidden_chars, '_', name)
    # 移除可能导致问题的首尾空格和点
    name = name.strip(' .')
    # 如果结果为空，返回默认名称
    if not name:
        name = "output"
    return name

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

def get_custom_name_from_video_info(work_dir: Path) -> str:
    """
    从 work_dir/videoInfo.json 中读取 tabName 和 uname，
    拼接成 "tabName by uname" 格式，并过滤非法字符。
    如果读取失败或字段为空，则返回 work_dir 的名称（即目录名）。
    """
    info_path = work_dir / "videoInfo.json"
    if not info_path.is_file():
        print(f"警告：{info_path} 不存在，使用目录名作为文件名基础。")
        return work_dir.name

    try:
        with open(info_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        tab_name = data.get("tabName")
        uname = data.get("uname")
        # 处理 null 或空字符串
        if not tab_name:
            tab_name = ""
        if not uname:
            uname = "未知UP主"
        if not tab_name:
            # 如果没有 tabName，只用目录名
            base = work_dir.name
        else:
            base = f"{tab_name} by {uname}"
        base = sanitize_filename(base)
        return base
    except Exception as e:
        print(f"警告：读取 {info_path} 失败 ({e})，使用目录名作为文件名基础。")
        return work_dir.name

def unique_filepath(target_dir: Path, base_name: str, ext: str = ".mp4") -> Path:
    """
    生成不重复的文件路径。如果 base_name+ext 已存在，则添加 _1, _2 等后缀。
    """
    stem = base_name
    counter = 1
    candidate = target_dir / f"{stem}{ext}"
    while candidate.exists():
        candidate = target_dir / f"{stem}_{counter}{ext}"
        counter += 1
    return candidate

# ---------- 核心处理函数 ----------
def process_m4s_files(work_dir: Path, target_dir: Path, raise_on_error: bool = True) -> bool:
    """
    在 work_dir 中处理两个 .m4s 文件，删除前9字节，合并为 mp4，
    并以 videoInfo.json 中的标题命名，移动到 target_dir。
    参数:
        work_dir: 包含原始 .m4s 和 videoInfo.json 的目录
        target_dir: 最终 mp4 文件存放的目录
        raise_on_error: True 时出错调用 sys.exit；False 时返回 False 并继续
    返回:
        bool: 成功返回 True，失败返回 False（仅在 raise_on_error=False 时有意义）
    """
    # 统一错误处理包装
    try:
        # 1. 检查 .m4s 文件数量
        m4s_files = [f for f in work_dir.glob("*.m4s") 
                     if f.name not in ("audio.m4s", "video.m4s")]
        if len(m4s_files) != 2:
            error_msg = f"错误：在目录 {work_dir} 下找到 {len(m4s_files)} 个 .m4s 文件，需要恰好 2 个。"
            print(error_msg)
            if raise_on_error:
                sys.exit(1)
            return False

        # 按实际流类型分派音视频（不能用文件名排序推断）
        audio_src, video_src = split_audio_video(m4s_files)

        # 2. 辅助函数：删除前9字节
        def strip_first_9_bytes(src: Path, dst: Path):
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

        audio_path = work_dir / "audio.m4s"
        video_path = work_dir / "video.m4s"
        strip_first_9_bytes(audio_src, audio_path)
        strip_first_9_bytes(video_src, video_path)
        print(f"完成前9字节删除，处理目录：{work_dir}")

        # 3. ffmpeg 合并
        # 显式指定流映射：视频取自 video.m4s、音频取自 audio.m4s。
        # 不依赖 ffmpeg 的自动流选择——自动选择恰好掩盖了音视频被错标的问题。
        output_mp4 = work_dir / "output.mp4"
        ffmpeg_cmd = [
            "ffmpeg", "-i", str(video_path), "-i", str(audio_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-strict", "experimental",
            "-y", str(output_mp4)
        ]
        print(f"执行命令: {' '.join(ffmpeg_cmd)}")
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print("ffmpeg 执行失败：")
            print(result.stderr)
            if raise_on_error:
                sys.exit(1)
            return False
        print(f"ffmpeg 合并成功：{output_mp4}")

        # 4. 生成自定义文件名并移动
        base_name = get_custom_name_from_video_info(work_dir)
        final_path = unique_filepath(target_dir, base_name, ".mp4")
        shutil.move(str(output_mp4), str(final_path))
        print(f"已移动并重命名：{final_path}")

        # 5. 删除临时文件
        for temp_file in (audio_path, video_path):
            try:
                if temp_file.exists():
                    temp_file.unlink()
                    print(f"已删除临时文件：{temp_file.name}")
            except Exception as e:
                print(f"警告：删除临时文件 {temp_file.name} 失败：{e}")

        return True

    except Exception as e:
        print(f"处理目录 {work_dir} 时发生未预期异常：{e}")
        if raise_on_error:
            sys.exit(1)
        return False

# ---------- 并行处理辅助 ----------
def get_optimal_thread_count(num_dirs: int) -> int:
    """
    计算线程数：
        - 最少 1
        - 最多为 CPU 逻辑核心数的 3/4 向下取整，并且不能超过需要处理的目录数量
    """
    cpu_count = multiprocessing.cpu_count()
    max_by_cpu = max(1, int(cpu_count * 3 / 4))
    return max(1, min(max_by_cpu, num_dirs))

def process_directories_parallel(directories, target_dir):
    """
    使用多线程并行处理多个目录。
    返回 (成功数, 失败数, 失败列表)
    """
    if not directories:
        return 0, 0, []
    
    # 过滤出有效目录
    valid_dirs = [d for d in directories if d.is_dir()]
    if not valid_dirs:
        return 0, 0, []
    
    thread_count = get_optimal_thread_count(len(valid_dirs))
    print(f"使用 {thread_count} 个线程并行处理 {len(valid_dirs)} 个目录...")
    
    success = 0
    failures = []
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        future_to_dir = {
            executor.submit(process_m4s_files, d, target_dir, False): d
            for d in valid_dirs
        }
        for future in as_completed(future_to_dir):
            d = future_to_dir[future]
            try:
                ok = future.result()
                if ok:
                    success += 1
                else:
                    failures.append(str(d))
            except Exception as e:
                print(f"处理目录 {d} 时线程异常：{e}")
                failures.append(str(d))
    
    return success, len(failures), failures

# ---------- 同步处理（用于单参数，保持原行为）----------
def process_directories_sync(directories, target_dir):
    """
    同步逐个处理多个目录，失败时继续。
    返回 (成功数, 失败数, 失败列表)
    """
    success = 0
    failures = []
    for d in directories:
        work_dir = Path(d).resolve()
        if not work_dir.is_dir():
            print(f"跳过无效目录：{d}")
            failures.append(str(d))
            continue
        print(f"\n>>> 正在处理目录：{work_dir}")
        ok = process_m4s_files(work_dir, target_dir, raise_on_error=False)
        if ok:
            success += 1
        else:
            failures.append(str(work_dir))
    return success, len(failures), failures

# ---------- 主程序 ----------
def main():
    # 显示提示信息（包含多线程说明）
    print("此文件用于将 B 站（bilibili.com）本地缓存的 .m4s 文件转换为可播放的 mp4 文件。")
    print("处理逻辑：")
    print("  1. 在每个缓存目录中找到两个 .m4s 文件，按文件内容识别出视频流与音频流，各自删除前 9 字节头部；")
    print("  2. 调用 ffmpeg 合并为 output.mp4；")
    print("  3. 根据 videoInfo.json 中的 tabName 和 up 主名称生成最终文件名；")
    print("  4. 将 mp4 文件移动到脚本执行目录。")
    print("\n多线程支持：")
    print("  - 当处理多个目录时（无参数自动扫描数字目录，或显式传入多个目录参数），")
    print("    会使用多线程并行转换，大幅提升速度。")
    print("  - 线程数自动设置为 CPU 逻辑核心数的 3/4（向下取整），同时不超过待处理目录总数。")
    print("  - 单目录模式（仅传入一个参数）保持原有的同步处理行为，便于调试。\n")

    parser = argparse.ArgumentParser(
        description="B站缓存视频转换工具 - 将 .m4s 分段转换为标准 mp4 文件，支持单目录和多目录并行处理。",
        epilog="示例：\n"
               "  %(prog)s                     # 自动扫描当前目录下所有数字命名的子目录，询问后并行转换\n"
               "  %(prog)s 12345               # 只处理目录 12345（同步模式）\n"
               "  %(prog)s 12345 67890         # 并行处理两个指定目录\n"
               "  %(prog)s /path/to/cache/dir  # 处理指定路径（同步模式）\n"
               "  %(prog)s --help              # 显示本帮助信息",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "directories",
        nargs="*",
        help="一个或多个包含两个 .m4s 文件的目录路径。\n"
             "• 如果不提供任何目录，将自动搜索当前目录下所有纯数字子目录（B站缓存目录特征），\n"
             "  并询问用户是否进行并行转换。\n"
             "• 如果提供一个目录，使用同步模式（传统行为），出错即退出。\n"
             "• 如果提供多个目录，使用多线程并行处理，单个目录失败不影响其他。"
    )
    args = parser.parse_args()

    target_dir = Path.cwd()          # 最终 mp4 存放目录（当前工作目录）
    dir_list = args.directories

    # 情况1：没有参数 -> 寻找所有数字命名的子目录，询问用户后处理（使用多线程）
    if len(dir_list) == 0:
        # 列出当前目录下所有纯数字子目录
        all_subdirs = [p for p in Path.cwd().iterdir() if p.is_dir() and p.name.isdigit()]
        if not all_subdirs:
            print("当前目录下没有找到任何数字命名的子目录（B站缓存目录通常为纯数字）。")
            sys.exit(0)

        print("即将对当前目录中所有数字目录中的缓存转换成mp4文件。")
        print("将处理的目录：")
        for d in all_subdirs:
            print(f"  {d.name}")
        answer = input("是否继续？(Y/N): ").strip().lower()
        if answer != 'y':
            print("用户取消操作。")
            sys.exit(0)

        # 并行处理
        success, fail_cnt, fail_list = process_directories_parallel(all_subdirs, target_dir)
        print(f"\n批量处理完成：成功 {success} 个，失败 {fail_cnt} 个。")
        if fail_cnt > 0:
            print("失败的目录：")
            for f in fail_list:
                print(f"  {f}")
        sys.exit(0)

    # 情况2：有一个或多个参数
    # 如果是单参数，保持原行为（出错立即退出，不使用多线程）
    if len(dir_list) == 1:
        work_dir = Path(dir_list[0]).resolve()
        if not work_dir.is_dir():
            print(f"错误：目录 '{work_dir}' 不存在或不是有效目录。")
            sys.exit(1)
        # 单个目录时 raise_on_error=True，出错会直接 sys.exit
        process_m4s_files(work_dir, target_dir, raise_on_error=True)
    else:
        # 多个参数，逐个验证有效性，然后并行处理
        valid_dirs = []
        for d in dir_list:
            p = Path(d).resolve()
            if p.is_dir():
                valid_dirs.append(p)
            else:
                print(f"警告：跳过无效目录 '{d}'")
        if not valid_dirs:
            print("没有有效的目录可处理。")
            sys.exit(1)
        success, fail_cnt, fail_list = process_directories_parallel(valid_dirs, target_dir)
        print(f"\n处理完成：成功 {success} 个，失败 {fail_cnt} 个。")
        if fail_cnt > 0:
            print("失败的目录：")
            for f in fail_list:
                print(f"  {f}")
        sys.exit(0 if fail_cnt == 0 else 1)

if __name__ == "__main__":
    main()