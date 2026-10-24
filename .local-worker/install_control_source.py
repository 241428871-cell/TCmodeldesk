"""Bind this installation's bundled control package into its own virtual environment."""

from __future__ import annotations

import argparse
import os
import sysconfig
from pathlib import Path
from uuid import uuid4

POINTER_NAME = "model_desk_control_source.pth"


def bind(source: Path, site_packages: Path | None = None) -> Path:
    """Atomically publish one plain-path ``.pth`` file for the bundled source."""
    source = source.resolve(strict=True)
    if "\n" in str(source) or "\r" in str(source):
        raise ValueError("控制内核路径包含换行，拒绝绑定")
    if not (source / "model_desk_control" / "__init__.py").is_file():
        raise ValueError("控制内核源码不完整")
    destination = site_packages or Path(sysconfig.get_path("purelib"))
    destination = destination.resolve(strict=True)
    target = destination / POINTER_NAME
    if target.is_symlink():
        raise ValueError("控制内核路径文件是链接，拒绝覆盖")
    content = f"{source}\n"
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        return target
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as output:
            _ = output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args(argv)
    try:
        target = bind(args.source)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"控制内核路径已绑定：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
