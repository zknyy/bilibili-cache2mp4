import os
import sys
import json
import re
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

        # 按文件名排序（字典序）
        m4s_files.sort(key=lambda f: f.name)
        smaller, larger = m4s_files[0], m4s_files[1]

        # 2. 辅助函数：删除前9字节
        def strip_first_9_bytes(src: Path, dst: Path):
            with open(src, 'rb') as fin:
                data = fin.read()
            if len(data) < 9:
                print(f"警告：文件 {src.name} 大小不足 9 字节，删除后将变为空文件。")
                content = b''
            else:
                content = data[9:]
            with open(dst, 'wb') as fout:
                fout.write(content)
            print(f"已处理：{src.name} -> {dst.name} (删除前9字节)")

        audio_path = work_dir / "audio.m4s"
        video_path = work_dir / "video.m4s"
        strip_first_9_bytes(smaller, audio_path)
        strip_first_9_bytes(larger, video_path)
        print(f"完成前9字节删除，处理目录：{work_dir}")

        # 3. ffmpeg 合并
        output_mp4 = work_dir / "output.mp4"
        ffmpeg_cmd = [
            "ffmpeg", "-i", str(video_path), "-i", str(audio_path),
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
    print("  1. 在每个缓存目录中找到两个 .m4s 文件（视频流和音频流），删除其前 9 字节头部；")
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