# -*- coding: utf-8 -*-
"""打包 game-update-plugin 为 zip（部署/分享用）。

用法:  python pack.py
输出:  game-update-plugin.zip（位于本目录上一级）
"""

import shutil
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

# 排除的自测/验证产物
EXCLUDE_DIRS = {"_selftest_out", "__pycache__", "node_modules"}
EXCLUDE_FILES = {"pack.py"}


def main() -> None:
    out_zip = ROOT.parent / f"{ROOT.name}.zip"
    if out_zip.exists():
        out_zip.unlink()

    # 收集要打包的文件
    files: list[pathlib.Path] = []
    for p in ROOT.rglob("*"):
        if p.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name in EXCLUDE_FILES:
            continue
        files.append(p)

    def _arcname(p: pathlib.Path) -> str:
        rel = p.relative_to(ROOT)
        # zip 内包含顶层目录名，解压即得 game-update-plugin/
        return f"{ROOT.name}/{rel.as_posix()}"

    with shutil.make_archive(str(out_zip)[:-4], "zip", root_dir=ROOT.parent, base_dir=ROOT.name) as _:
        pass

    # make_archive 已生成，重新校验内容（列出清单）
    import zipfile
    with zipfile.ZipFile(out_zip) as zf:
        names = zf.namelist()
    print(f"打包完成: {out_zip}")
    print(f"共 {len(names)} 个文件:")
    for n in sorted(names):
        print(f"  {n}")


if __name__ == "__main__":
    main()
