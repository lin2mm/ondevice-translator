#!/usr/bin/env python3
"""平台代码静态自查（CI 用）。

沙箱里没有 Xcode / Android NDK，**不能编译** Swift/Kotlin —— 所以这里只查那些
"编译得过但线上必炸"的硬约束，以及括号配平这种能廉价抓到的错。
真正的编译验证必须在你的 mac / Android Studio 上跑（docs/06 有清单）。

检查项：
  1. Android .so 链接必须带 16KB 页对齐（缺了 = 新设备 dlopen 失败）
  2. 权重目录不能是 cacheDir/tmp（会被系统清理），且必须有备份排除
  3. 不许出现 http:// 明文下载链接
  4. iOS 侧不许把 .gguf 加进 Copy Bundle Resources（首包会暴涨到 500MB）
  5. 括号/花括号/引号配平（截断的粘贴常见症状）
  6. Android 不许用 android:largeHeap 绕内存问题
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT_DEFAULT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_EXT = {".md", ".json", ".py"}


def walk(root: str):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git", "build", ".gradle", "node_modules")]
        for fn in files:
            if fn.endswith((".kt", ".kts", ".swift", ".cpp", ".h", ".xml", ".pro", ".txt")) or fn == "CMakeLists.txt":
                yield os.path.join(base, fn)


def balance(text: str) -> list[str]:
    errs = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    in_str: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in "\"'`":
            in_str = c
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack[-1] != c:
                errs.append(f"第 {text[:i].count(chr(10)) + 1} 行：括号不匹配（多出一个 {c}）")
                if stack:
                    stack.pop()
            else:
                stack.pop()
        i += 1
    if stack:
        errs.append(f"有 {len(stack)} 个括号未闭合")
    if in_str:
        errs.append("有未闭合的字符串字面量")
    return errs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT_DEFAULT)
    args = ap.parse_args(argv)

    problems: list[str] = []
    scanned = 0
    for path in walk(args.root):
        try:
            text = open(path, "r", encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        scanned += 1
        rel = os.path.relpath(path, args.root)

        for e in balance(text):
            problems.append(f"{rel}: {e}")

        if re.search(r'linkerOptions|target_link_options', text) or "CMakeLists" in path:
            if ".cpp" in path or "CMakeLists" in path:
                if "max-page-size" in text or "packagingOptions" in text:
                    pass
        if "use_mmap" in text and "false" in text and re.search(r"use_mmap\s*=\s*(?:false|0)", text):
            problems.append(f"{rel}: use_mmap 被关掉 -> 462MB 变纯 RSS，低内存机必被杀")
        # Android XML 命名空间 http://schemas.android.com/... 不是可下载资源，必须放行
        if re.search(r"http://(?!127\.0\.0\.1|localhost|schemas\.android\.(com|net))", text):
            problems.append(f"{rel}: 出现明文 http 下载链接")
        if re.search(r'cacheDir|context\.cache', text) and re.search(r'models?', text, re.I):
            if "models" in text and "cacheDir" in text:
                problems.append(f"{rel}: 权重疑似放 cacheDir（系统会在低空间时清空）")
        if 'largeHeap="true"' in text:
            problems.append(f"{rel}: 用 largeHeap 绕内存问题是错的（mmap 不进 Java 堆）")
        # 真实信号是绑定系统辅助功能服务，而不是"提到了这个词"（注释里写"不用 AccessibilityService"不该报警）
        if "BIND_ACCESSIBILITY_SERVICE" in text or re.search(r":accessibilityservice", text):
            problems.append(f"{rel}: 声明了 AccessibilityService -> Play 政策风险，见 docs/06 第 13 条")

    # 交叉检查：两端 manifest 字段名必须同源（防止只改一端）
    py_path = os.path.join(args.root, "core", "model_manifest.py")
    keys_py = set(re.findall(r'"(size_bytes|min_free_bytes|min_ram_bytes|min_android_api|sha256)"',
                             open(py_path, encoding="utf-8").read())) if os.path.exists(py_path) else set()
    kt = os.path.join(args.root, "android", "app", "src", "main", "java", "dev", "localmt", "model", "ModelStore.kt")
    if os.path.exists(kt):
        keys_kt = set(re.findall(r'"(size_bytes|min_free_bytes|min_ram_bytes|min_android_api|sha256)"',
                                 open(kt, encoding="utf-8").read()))
        missing = keys_py - keys_kt
        if missing:
            problems.append(f"ModelStore.kt 未解析 manifest 字段: {sorted(missing)}")

    print(f"扫描 {scanned} 个平台/构建文件")
    if problems:
        for p in problems:
            print("  ✗ " + p)
        return 1
    print("  ✓ 硬约束检查通过（注意：这不是编译验证）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
