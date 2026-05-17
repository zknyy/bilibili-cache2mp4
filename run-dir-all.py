import os
import sys
import argparse
import subprocess
import shutil
from pathlib import Path

def process_m4s_files(work_dir: Path, target_dir: Path):
    """在 work_dir 目录下处理两个 .m4s 文件，合并后移动到 target_dir"""
    # 获取工作目录下所有 .m4s 文件（排除已生成的目标文件）
    m4s_files = [f for f in work_dir.glob("*.m4s") 
                 if f.name not in ("audio.m4s", "video.m4s")]
    
    if len(m4s_files) != 2:
        print(f"错误：在目录 {work_dir} 下找到 {len(m4s_files)} 个 .m4s 文件，需要恰好 2 个。")
        sys.exit(1)
    
    # 按文件名排序（字典序）
    m4s_files.sort(key=lambda f: f.name)
    smaller, larger = m4s_files[0], m4s_files[1]
    
    def strip_first_9_bytes(src: Path, dst: Path):
        """读取 src 文件，删除前 9 个字节后写入 dst 文件"""
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
    
    # 目标文件也放在同一工作目录
    audio_path = work_dir / "audio.m4s"
    video_path = work_dir / "video.m4s"
    strip_first_9_bytes(smaller, audio_path)
    strip_first_9_bytes(larger, video_path)
    
    print(f"完成前9字节删除，处理目录：{work_dir}")
    
    # 调用 ffmpeg 合并
    output_mp4 = work_dir / "output.mp4"
    ffmpeg_cmd = [
        "ffmpeg", "-i", str(video_path), "-i", str(audio_path),
        "-c:v", "copy", "-c:a", "aac", "-strict", "experimental",
        "-y", str(output_mp4)   # -y 覆盖已有文件
    ]
    print(f"执行命令: {' '.join(ffmpeg_cmd)}")
    try:
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print("ffmpeg 执行失败：")
            print(result.stderr)
            sys.exit(1)
        print(f"ffmpeg 合并成功：{output_mp4}")
    except FileNotFoundError:
        print("错误：未找到 ffmpeg 命令，请确保 ffmpeg 已安装并加入 PATH。")
        sys.exit(1)
    
    # 移动 output.mp4 到当前工作目录（执行脚本的目录）
    target_path = target_dir / "output.mp4"
    try:
        shutil.move(str(output_mp4), str(target_path))
        print(f"已移动 output.mp4 到：{target_path}")
    except Exception as e:
        print(f"移动文件失败：{e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(
        description="删除两个 .m4s 文件的前9字节，分别保存为 audio.m4s 和 video.m4s，然后用 ffmpeg 合并为 output.mp4，并移动到当前目录。"
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
    
    # 当前工作目录（执行脚本时的目录）
    current_dir = Path.cwd()
    
    process_m4s_files(work_dir, current_dir)

if __name__ == "__main__":
    main()