"""Create a drag-to-Applications disk image from an already verified local app."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument('app', type=Path)
    _ = parser.add_argument('output', type=Path)
    args = parser.parse_args()
    app: Path = args.app.resolve()
    output: Path = args.output.resolve()
    if not (app / 'Contents/MacOS/TCworkdesk').is_file() or output.exists():
        raise SystemExit('应用缺失或输出已存在；不会覆盖已有安装镜像')
    _ = subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True, timeout=45)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tcworkdesk-dmg-') as temporary:
        stage = Path(temporary)
        _ = subprocess.run(['/usr/bin/ditto', str(app), str(stage / app.name)], check=True, timeout=90)
        (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
        _ = (stage / '安装说明.txt').write_text(
            '将 TCworkdesk.app 拖入 Applications，然后从应用程序打开。\n'
            '应用自带 Python 和 Node；Codex、DSH、Claude 使用电脑已有安装。\n'
            '菜单 TCworkdesk → 数据位置与迁移，可以选择新路径并迁移应用数据。\n'
            '默认数据在系统盘用户 Library/Application Support/TCworkdesk/Data。\n'
            '项目源码与 .model-desk 项目记录随项目保留，不移动；迁移原目录保留为备份。\n'
            '连接已有工作台请使用应用菜单，原账户和费用授权保持原样。\n'
            '本机 Apple Silicon 构建；采用本地签名，尚未进行 Developer ID 公证。\n', encoding='utf-8')
        _ = subprocess.run(['/usr/bin/hdiutil', 'create', '-volname', 'TCworkdesk', '-srcfolder',
            str(stage), '-format', 'UDZO', '-fs', 'HFS+', str(output)], check=True, timeout=180)
    print(output)


if __name__ == '__main__':
    main()
