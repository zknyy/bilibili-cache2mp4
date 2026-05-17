import os
import sys
import argparse
from pathlib import Path

def process_m4s_files(work_dir: Path):
    """在 work_dir 目录下处理两个 .m4s 文件"""
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
    
    # 目标文件也放在同一目录
    strip_first_9_bytes(smaller, work_dir / "audio.m4s")
    strip_first_9_bytes(larger, work_dir / "video.m4s")
    
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