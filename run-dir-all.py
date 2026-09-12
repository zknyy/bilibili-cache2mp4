import os
import sys
import json
import re
import struct
import argparse
import subprocess
import shutil
import threading
from pathlib import Path
from dataclasses import dataclass
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


def _collect_media_info(fp, start, end, info, depth=0):
    """递归遍历 [start, end) 的 box，收集各 hdlr 的 handler_type 与 mvhd 时长。"""
    if depth > MAX_BOX_DEPTH:
        return
    offset = start
    while offset + 8 <= end:
        fp.seek(offset)
        header = fp.read(8)
        if len(header) < 8:
            return
        size = struct.unpack(">I", header[:4])[0]
        box_type = header[4:8].decode("latin-1", "replace")
        header_size = 8
        if size == 1:                      # 64 位长度
            extra = fp.read(8)
            if len(extra) < 8:
                return
            size = struct.unpack(">Q", extra)[0]
            header_size = 16
        elif size == 0:                    # 一直延伸到文件末尾
            size = end - offset
        if size < header_size or offset + size > end:
            return                         # 结构异常，放弃后续解析
        if box_type == "hdlr":
            payload = fp.read(min(12, size - header_size))
            if len(payload) >= 12:
                # hdlr 布局：version+flags(4) + pre_defined(4) + handler_type(4)
                info["handlers"].append(payload[8:12].decode("latin-1", "replace"))
        elif box_type == "mvhd":
            # mvhd 布局（version+flags 之后）：
            #   v0 -> creation(4) modification(4) timescale(4) duration(4)
            #   v1 -> creation(8) modification(8) timescale(4) duration(8)
            payload = fp.read(min(32, size - header_size))
            if len(payload) >= 20:
                if payload[0] == 1 and len(payload) >= 32:
                    timescale = struct.unpack(">I", payload[20:24])[0]
                    duration = struct.unpack(">Q", payload[24:32])[0]
                else:
                    timescale = struct.unpack(">I", payload[12:16])[0]
                    duration = struct.unpack(">I", payload[16:20])[0]
                if timescale > 0:
                    info["duration"] = duration / timescale
        if box_type in CONTAINER_BOXES:
            _collect_media_info(fp, offset + header_size, offset + size, info, depth + 1)
        offset += size


def probe_mp4(path):
    """
    解析 MP4/m4s 的结构，返回 {"handlers": [...], "duration": 秒 或 None}。
    无法解析时 handlers 为空列表。同时兼容已剥头的文件与 B站原始缓存文件。
    """
    try:
        end = path.stat().st_size
        with open(path, "rb") as fp:
            # 0 = 已剥头的文件；9 = B站原始缓存文件
            for start in (0, BILIBILI_PREFIX_BYTES):
                if start >= end:
                    continue
                candidate = {"handlers": [], "duration": None}
                _collect_media_info(fp, start, end, candidate)
                if candidate["handlers"]:
                    return candidate
    except OSError:
        pass
    return {"handlers": [], "duration": None}


def detect_media_kind(path):
    """判断 .m4s 是视频流还是音频流，返回 'video' / 'audio' / None（无法判定）。"""
    handlers = probe_mp4(path)["handlers"]
    if not handlers:
        return None
    first = handlers[0]
    if first == "vide":
        return "video"
    if first == "soun":
        return "audio"
    return None


# ---------- 已存在输出文件的校验（避免重复生成）----------
MIN_VALID_MP4_BYTES = 1024          # 小于此体积必定不是完整视频
DURATION_TOLERANCE_SECONDS = 3.0    # 与 videoInfo.json 时长的允许偏差（秒）
DURATION_TOLERANCE_RATIO = 0.03     # 或按时长比例，取两者中较大的一个


def _box_layout_complete(fp, end):
    """
    检查顶层 box 是否恰好铺满整个文件。

    被截断的 mp4 仅靠 moov（位于文件开头）是发现不了的——它的流信息和时长都
    还读得到，但最后一个 box 声明的长度会超出文件末尾，这里就是凭这一点识破。
    """
    offset = 0
    while offset + 8 <= end:
        fp.seek(offset)
        header = fp.read(8)
        if len(header) < 8:
            return False
        size = struct.unpack(">I", header[:4])[0]
        header_size = 8
        if size == 1:                      # 64 位长度
            extra = fp.read(8)
            if len(extra) < 8:
                return False
            size = struct.unpack(">Q", extra)[0]
            header_size = 16
        elif size == 0:                    # 延伸到文件末尾，视为完整
            size = end - offset
        if size < header_size or offset + size > end:
            return False                   # 声明长度越过文件末尾 → 被截断
        offset += size
    return offset == end


def check_existing_output(path: Path, expected_duration=None):
    """
    检查目标位置上已存在的文件是否完好，返回值：
        None        - 文件不存在
        'valid'     - 是完整的 mp4：体积正常、结构完整、含视频流与音频流，
                      且时长与预期相符
        'invalid'   - 存在但不是完整 mp4（体积过小、被截断、结构损坏、缺少音视频流）
        'different' - 是完好的 mp4，但时长与预期明显不符（同名却是另一个视频）
    """
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
    except OSError:
        return None

    if size < MIN_VALID_MP4_BYTES:
        return "invalid"               # 空文件或半成品

    try:
        with open(path, "rb") as fp:
            if not _box_layout_complete(fp, size):
                return "invalid"       # 被截断或结构损坏
    except OSError:
        return None

    info = probe_mp4(path)
    handlers = info["handlers"]
    if "vide" not in handlers or "soun" not in handlers:
        return "invalid"               # 缺流：被截断或压根不是视频

    duration = info["duration"]
    if expected_duration and duration:
        tolerance = max(DURATION_TOLERANCE_SECONDS,
                        expected_duration * DURATION_TOLERANCE_RATIO)
        if abs(duration - expected_duration) > tolerance:
            return "different"
    return "valid"


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

# ---------- videoInfo.json 读取与输出规划 ----------
UNKNOWN_UP_NAME = "未知UP主"


def read_video_info(work_dir: Path):
    """
    读取 work_dir/videoInfo.json，返回 dict；文件缺失或损坏时返回 None。
    """
    info_path = work_dir / "videoInfo.json"
    if not info_path.is_file():
        return None
    try:
        with open(info_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f"警告：读取 {info_path} 失败 ({e})，将改用目录名。")
        return None


def get_series_key(info):
    """
    取视频所属系列的标识。B站把同一合集/多P视频的 groupId 设为相同值，
    因此用 groupId 作为系列标识；缺失时退回 groupTitle。
    返回 None 表示该视频无法归入任何系列。
    """
    if not info:
        return None
    group_id = (info.get("groupId") or "").strip()
    if group_id:
        return f"id:{group_id}"
    group_title = (info.get("groupTitle") or "").strip()
    if group_title:
        return f"title:{group_title}"
    return None


def flat_base_name(work_dir: Path, info) -> str:
    """
    非系列视频的文件名基础："tabName by uname"；读不到 videoInfo.json 时退回目录名。
    """
    if not info:
        print(f"警告：{work_dir / 'videoInfo.json'} 不可用，使用目录名作为文件名基础。")
        return sanitize_filename(work_dir.name)
    tab_name = (info.get("tabName") or "").strip()
    uname = (info.get("uname") or "").strip() or UNKNOWN_UP_NAME
    if not tab_name:
        # 如果没有 tabName，只用目录名
        return sanitize_filename(work_dir.name)
    return sanitize_filename(f"{tab_name} by {uname}")


@dataclass
class OutputPlan:
    """一个缓存目录的输出规划。"""
    dest_dir: Path                    # 最终存放 mp4 的目录
    base_name: str                    # 不含扩展名的文件名
    series_dir: str = ""              # 系列目录名；为空表示该视频不属于多视频系列
    expected_duration: float = None   # videoInfo.json 声明的时长（秒），用于校验已有文件

    @property
    def planned_path(self) -> Path:
        """预判的输出全路径（未考虑重名避让）。"""
        return self.dest_dir / f"{self.base_name}.mp4"


def build_output_plans(work_dirs, target_dir: Path):
    """
    为一整批缓存目录规划输出位置。

    同一系列（videoInfo.json 中 groupId 相同）在本批次中出现 2 个及以上视频时，
    这些视频统一放进 target_dir/{系列名称}-{up主名称}/ 子目录，文件名只用视频
    名称（tabName）。其余视频维持原有行为：直接放在 target_dir，文件名为
    "tabName by uname"。

    参数:
        work_dirs: 本批次要处理的缓存目录列表
        target_dir: 最终 mp4 的根存放目录
    返回:
        dict: work_dir -> OutputPlan
    """
    infos = {d: read_video_info(d) for d in work_dirs}

    # 统计每个系列在本批次中出现的视频数量
    series_members = {}
    for d in work_dirs:
        key = get_series_key(infos[d])
        if key:
            series_members.setdefault(key, []).append(d)

    plans = {}
    for d in work_dirs:
        info = infos[d]
        key = get_series_key(info)
        members = series_members.get(key, []) if key else []
        duration = info.get("duration") if info else None
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        if len(members) >= 2:
            # 系列目录名 = 系列名称-up主名称
            group_title = (info.get("groupTitle") or "").strip()
            uname = (info.get("uname") or "").strip() or UNKNOWN_UP_NAME
            if group_title:
                series_dir = sanitize_filename(f"{group_title}-{uname}")
            else:
                # 没有系列名称时退化为 up主名称，保证仍能分组
                series_dir = sanitize_filename(f"{uname}-合集")
            # 系列内的视频只用视频名称，不追加 up主名称
            tab_name = (info.get("tabName") or "").strip()
            base_name = sanitize_filename(tab_name) if tab_name else sanitize_filename(d.name)
            plans[d] = OutputPlan(target_dir / series_dir, base_name, series_dir, duration)
        else:
            plans[d] = OutputPlan(target_dir, flat_base_name(d, info), "", duration)
    return plans


def describe_plans(plans, target_dir: Path):
    """打印本次批处理的输出规划，让系列分组一目了然。"""
    series_dirs = {}
    singles = []
    for work_dir, plan in plans.items():
        if plan.series_dir:
            series_dirs.setdefault(plan.series_dir, []).append((work_dir, plan))
        else:
            singles.append((work_dir, plan))

    if series_dirs:
        total = sum(len(v) for v in series_dirs.values())
        print(f"\n检测到 {len(series_dirs)} 个系列，共 {total} 个视频，各自放入独立目录：")
        for series_dir in sorted(series_dirs):
            print(f"  {target_dir / series_dir}")
            for _, plan in sorted(series_dirs[series_dir], key=lambda x: x[1].base_name):
                print(f"      {plan.base_name}.mp4{describe_existing(plan)}")
    if singles:
        print(f"\n其余 {len(singles)} 个视频不属于多视频系列，直接输出到 {target_dir}：")
        for _, plan in sorted(singles, key=lambda x: x[1].base_name):
            print(f"  {plan.base_name}.mp4{describe_existing(plan)}")
    print()


def describe_existing(plan: OutputPlan) -> str:
    """预判目标位置已有文件的状态，用于在处理前提示是否会跳过。"""
    status = check_existing_output(plan.planned_path, plan.expected_duration)
    if status == "valid":
        return "  [已存在且完好，跳过]"
    if status == "invalid":
        return "  [已存在但不完好，将覆盖重做]"
    if status == "different":
        return "  [同名文件是另一个视频，将另存为新名字]"
    return ""

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


# 并行处理时保护“定名 + 移动”这一步，避免同批次内同名文件互相覆盖
_OUTPUT_LOCK = threading.Lock()


def resolve_output(plan: OutputPlan):
    """
    生成前预判输出位置，返回 (planned_path, action)：
        'skip'      - 目标文件已存在且校验通过，无需重新生成
        'overwrite' - 目标文件存在但不完好（损坏/半成品），应原地覆盖重做
        'avoid'     - 同名文件是另一个完好的视频，应改用带序号的新名字
        'new'       - 目标位置空闲，直接使用 planned_path
    """
    planned = plan.planned_path
    status = check_existing_output(planned, plan.expected_duration)
    if status == "valid":
        return planned, "skip"
    if status == "invalid":
        return planned, "overwrite"
    if status == "different":
        return planned, "avoid"
    return planned, "new"

# ---------- 核心处理函数 ----------
def process_m4s_files(work_dir: Path, plan: OutputPlan, raise_on_error: bool = True) -> str:
    """
    在 work_dir 中处理两个 .m4s 文件，删除前9字节，合并为 mp4，
    并按 plan 指定的目录与文件名输出。

    生成前会先预判目标文件：若已存在且校验通过（体积、音视频流、时长均正常），
    则直接跳过，不重复生成。

    参数:
        work_dir: 包含原始 .m4s 和 videoInfo.json 的目录
        plan: OutputPlan，指定最终存放目录与文件名
        raise_on_error: True 时出错调用 sys.exit；False 时返回 'failed' 并继续
    返回:
        str: 'generated'（新生成）/ 'skipped'（已存在，跳过）/ 'failed'（失败）
    """
    # 统一错误处理包装
    try:
        # 0. 预判输出位置：已存在且完好则跳过，避免重复生成
        final_path, action = resolve_output(plan)
        if action == "skip":
            try:
                size_mb = final_path.stat().st_size / 1024 / 1024
            except OSError:
                size_mb = 0.0
            print(f"跳过：已存在且校验通过 {final_path} ({size_mb:.2f} MB)")
            return "skipped"

        # 1. 检查 .m4s 文件数量
        m4s_files = [f for f in work_dir.glob("*.m4s") 
                     if f.name not in ("audio.m4s", "video.m4s")]
        if len(m4s_files) != 2:
            error_msg = f"错误：在目录 {work_dir} 下找到 {len(m4s_files)} 个 .m4s 文件，需要恰好 2 个。"
            print(error_msg)
            if raise_on_error:
                sys.exit(1)
            return "failed"

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
            return "failed"
        print(f"ffmpeg 合并成功：{output_mp4}")

        # 4. 按规划输出：系列视频放进同名子目录，其余直接放到根目录
        #    定名与移动放在同一把锁内，避免并行处理时同名文件互相覆盖
        plan.dest_dir.mkdir(parents=True, exist_ok=True)
        with _OUTPUT_LOCK:
            if action == "overwrite":
                # 目标位置上的旧文件已确认不完好，删掉后原地重做
                try:
                    final_path.unlink()
                    print(f"已删除不完好的旧文件：{final_path.name}")
                except OSError as e:
                    print(f"警告：删除旧文件 {final_path.name} 失败：{e}，改用新的文件名。")
                    final_path = unique_filepath(plan.dest_dir, plan.base_name, ".mp4")
            else:
                # 'new' / 'avoid'：取一个当前空闲的名字（'avoid' 会拿到 _1 后缀）
                final_path = unique_filepath(plan.dest_dir, plan.base_name, ".mp4")
            full_path = str(final_path)
            if len(full_path) > 250:
                print(f"警告：输出路径长度 {len(full_path)} 字符，接近 Windows MAX_PATH(260) 限制，"
                      f"若移动失败请改在更短的路径下运行。")
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

        return "generated"

    except Exception as e:
        print(f"处理目录 {work_dir} 时发生未预期异常：{e}")
        if raise_on_error:
            sys.exit(1)
        return "failed"

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
    返回 (新生成数, 跳过数, 失败数, 失败列表)
    """
    if not directories:
        return 0, 0, 0, []
    
    # 过滤出有效目录
    valid_dirs = [d for d in directories if d.is_dir()]
    if not valid_dirs:
        return 0, 0, 0, []
    
    thread_count = get_optimal_thread_count(len(valid_dirs))
    print(f"使用 {thread_count} 个线程并行处理 {len(valid_dirs)} 个目录...")

    # 先统一规划输出位置：同一系列的多个视频会被放进同一个子目录
    plans = build_output_plans(valid_dirs, target_dir)
    describe_plans(plans, target_dir)

    generated = 0
    skipped = 0
    failures = []
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        future_to_dir = {
            executor.submit(process_m4s_files, d, plans[d], False): d
            for d in valid_dirs
        }
        for future in as_completed(future_to_dir):
            d = future_to_dir[future]
            try:
                status = future.result()
            except Exception as e:
                print(f"处理目录 {d} 时线程异常：{e}")
                failures.append(str(d))
                continue
            if status == "generated":
                generated += 1
            elif status == "skipped":
                skipped += 1
            else:
                failures.append(str(d))
    
    return generated, skipped, len(failures), failures

# ---------- 同步处理（用于单参数，保持原行为）----------
def process_directories_sync(directories, target_dir):
    """
    同步逐个处理多个目录，失败时继续。
    返回 (新生成数, 跳过数, 失败数, 失败列表)
    """
    generated = 0
    skipped = 0
    failures = []
    valid_dirs = []
    for d in directories:
        work_dir = Path(d).resolve()
        if not work_dir.is_dir():
            print(f"跳过无效目录：{d}")
            failures.append(str(d))
            continue
        valid_dirs.append(work_dir)

    plans = build_output_plans(valid_dirs, target_dir)
    describe_plans(plans, target_dir)

    for work_dir in valid_dirs:
        print(f"\n>>> 正在处理目录：{work_dir}")
        status = process_m4s_files(work_dir, plans[work_dir], raise_on_error=False)
        if status == "generated":
            generated += 1
        elif status == "skipped":
            skipped += 1
        else:
            failures.append(str(work_dir))
    return generated, skipped, len(failures), failures

# ---------- 主程序 ----------
def main():
    # 显示提示信息（包含多线程说明）
    print("此文件用于将 B 站（bilibili.com）本地缓存的 .m4s 文件转换为可播放的 mp4 文件。")
    print("处理逻辑：")
    print("  1. 在每个缓存目录中找到两个 .m4s 文件，按文件内容识别出视频流与音频流，各自删除前 9 字节头部；")
    print("  2. 调用 ffmpeg 合并为 output.mp4；")
    print("  3. 根据 videoInfo.json 中的 tabName 和 up 主名称生成最终文件名；")
    print("  4. 将 mp4 文件移动到脚本执行目录。")
    print("\n避免重复生成：")
    print("  - 生成前先预判目标位置与文件名；")
    print("  - 若该文件已存在且校验通过（体积正常、含视频流与音频流、时长与")
    print("    videoInfo.json 相符），则直接跳过，不再重复转换；")
    print("  - 已存在但校验不通过（损坏/半成品）时会覆盖重做；")
    print("  - 同名文件若是另一个完好的视频，则另存为新名字，不会覆盖它。")
    print("\n系列视频处理：")
    print("  - 同一系列的多个视频（videoInfo.json 中 groupId 相同的缓存目录），")
    print("    会统一放进一个子目录，目录名为「系列名称-up主名称」；")
    print("  - 系列内的视频文件只使用视频名称，不再追加 up 主名称；")
    print("  - 系列内只有一个视频时不建子目录，其余视频也直接放在脚本执行目录。")
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
        generated, skipped, fail_cnt, fail_list = process_directories_parallel(all_subdirs, target_dir)
        print(f"\n批量处理完成：成功 {generated + skipped} 个"
              f"（新生成 {generated}，跳过已存在 {skipped}），失败 {fail_cnt} 个。")
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
        # 单目录模式下本批次只有它自己，无法判断是否属于系列，因此维持原有命名
        status = process_m4s_files(work_dir, build_output_plans([work_dir], target_dir)[work_dir],
                                   raise_on_error=True)
        print(f"\n处理完成：{'已跳过（文件已存在且完好）' if status == 'skipped' else '已生成'}。")
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
        success, skipped, fail_cnt, fail_list = process_directories_parallel(valid_dirs, target_dir)
        print(f"\n处理完成：成功 {success + skipped} 个"
              f"（新生成 {success}，跳过已存在 {skipped}），失败 {fail_cnt} 个。")
        if fail_cnt > 0:
            print("失败的目录：")
            for f in fail_list:
                print(f"  {f}")
        sys.exit(0 if fail_cnt == 0 else 1)

if __name__ == "__main__":
    main()