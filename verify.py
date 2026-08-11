# -*- coding: utf-8 -*-
"""game-update-watcher 一键验证（跨平台，不依赖 PowerShell 语法）。

用法: python verify.py
执行四步检查: 环境 -> 语法 -> 配置 -> 真实采集+出图
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent

PASS = 0
FAIL = 0


def report(ok: bool, msg: str) -> None:
    global PASS, FAIL
    tag = "[OK]  " if ok else "[FAIL]"
    print(f"{tag} {msg}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("\n========== 1/4 环境检查 ==========")

    # Python 版本
    ver = sys.version_info
    report(ver >= (3, 10), f"Python {ver.major}.{ver.minor}.{ver.micro} (需要 >= 3.10)")

    # 依赖
    missing = []
    for mod in ("httpx", "PIL"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        report(False, f"缺少依赖: {', '.join(missing)}，请执行: pip install maibot-plugin-sdk httpx pillow")
    else:
        import httpx
        import PIL
        report(True, f"httpx {httpx.__version__} / pillow {PIL.__version__}")

    print("\n========== 2/4 语法检查 ==========")
    py_files = sorted(ROOT.rglob("*.py"))
    bad = []
    for f in py_files:
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{f.name}: {e}")
    report(not bad, f"{len(py_files)} 个 .py 文件语法通过" if not bad else f"语法错误: {'; '.join(bad)}")

    print("\n========== 3/4 配置检查 ==========")
    json_files = sorted((ROOT / "games").glob("*.json"))
    bad_json = []
    for f in json_files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if "adapter" not in d:
                bad_json.append(f"{f.name}: 缺少 adapter 字段")
            if "format" not in d:
                bad_json.append(f"{f.name}: 缺少 format 字段")
        except json.JSONDecodeError as e:
            bad_json.append(f"{f.name}: {e}")
    # formats 模板检查
    fmt_files = sorted((ROOT / "formats").glob("*.json"))
    fmt_names = {f.stem for f in fmt_files}
    for f in json_files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("format") not in fmt_names:
                bad_json.append(f"{f.name}: format '{d.get('format')}' 不存在于 formats/")
        except Exception:
            pass
    report(not bad_json, f"{len(json_files)} 个游戏配置 + {len(fmt_files)} 个格式模板可解析" if not bad_json else f"配置错误: {'; '.join(bad_json)}")

    print("\n========== 4/4 真实采集测试（联网，约 10~30 秒） ==========")
    os.chdir(ROOT)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(ROOT / "selftest.py")], env=env)
    report(proc.returncode == 0, "selftest.py 执行完成")

    print("\n========== 汇总 ==========")
    if FAIL:
        print(f"{PASS} 项通过, {FAIL} 项失败 —— 请把上方 [FAIL] 输出贴回来")
        return 1
    print(f"{PASS} 项全部通过")
    print("最终检查: 打开 _selftest_out/ 下的 PNG，确认文字无乱码、版式正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
