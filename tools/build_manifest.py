#!/usr/bin/env python3
"""从本地权重目录生成 manifest.json（sha256 与体积全部实测，禁止手填）。

用法:
  python3 tools/build_manifest.py --dir ./out --base-url https://cdn.example.com/models \
      --version 2026.09.05-a --out ios/LocalMT/Resources/manifest.json \
      --copy-to android/app/src/main/assets/manifest.json

发布前它会做三件别人不做的事：
  1. 实测 sha256/体积（手填错一个字符 = 用户端永久校验失败）
  2. 跑 core.model_manifest.Manifest.validate()（字段拼错、http 链接、语言不含 zh/en、磁盘余量算小了）
  3. 校验 --base-url 每个 asset 真的可取（HEAD，比对 content-length；离线时跳过并警告）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.model_manifest import Manifest, fmt_size, scan_dir_to_manifest  # noqa: E402

DEFAULTS = {
    "Hy-MT2-1.8B-1.25Bit": {
        "min_ram_bytes": 3_000_000_000, "min_free_bytes": 520_000_000,
        "languages": ["zh", "en"], "engine": "llama-cpp", "license": "Apache-2.0",
        "supports": ["text", "terminology", "style", "structured"], "tier": 1,
        "notes": "默认档：462MB，质量/体积最优（腾讯 Hunyuan Hy-MT2, Apache-2.0）",
    },
    "Hy-MT2-1.8B-Q4_K_M": {
        "min_ram_bytes": 5_000_000_000, "min_free_bytes": 1_250_000_000,
        "languages": ["zh", "en"], "engine": "llama-cpp", "license": "Apache-2.0",
        "supports": ["text", "terminology", "style", "structured"], "tier": 2,
        "notes": "质量档：1133MB，8GB+ 机型可选",
    },
    "Hy-MT2-1.8B-2bit": {
        "min_ram_bytes": 3_000_000_000, "min_free_bytes": 660_000_000,
        "languages": ["zh", "en"], "engine": "llama-cpp", "license": "Apache-2.0", "tier": 1,
        "supports": ["text", "terminology"],
        "notes": "2bit 变体：601MB，Arm SME2 设备更快",
    },
}


def check_urls(m: Manifest, timeout: float = 12.0) -> list[str]:
    problems: list[str] = []
    for a in m.assets:
        if not a.url.startswith(("http://", "https://")):
            continue  # bundle:// 等 App 内占位无 CDN 可 HEAD；urlopen 对未知 scheme 抛未捕获的 ValueError
        req = urllib.request.Request(a.url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                clen = r.headers.get("Content-Length")
                if clen is None:
                    problems.append(f"{a.id}: CDN 没给 Content-Length（无法断点续传）")
                elif int(clen) != a.size_bytes:
                    problems.append(f"{a.id}: 线上体积 {fmt_size(int(clen))} != manifest {fmt_size(a.size_bytes)}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            problems.append(f"{a.id}: HEAD 失败（{exc}）—— 离线构建时忽略")
    return problems


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", required=True, help="含 .gguf/.task/.litertlm/.mnn 的目录")
    p.add_argument("--base-url", required=True)
    p.add_argument("--version", required=True, help="发布号，例如 2026.09.05-a")
    p.add_argument("--out", required=True)
    p.add_argument("--copy-to", action="append", default=[])
    p.add_argument("--extra", help="覆盖默认参数的 json 文件")
    p.add_argument("--skip-url-check", action="store_true")
    p.add_argument("--prune-to", type=int, default=0, help="只保留前 N 个 tier 最小的 asset（首发建议 1）")
    args = p.parse_args(argv)

    extra = dict(DEFAULTS)
    if args.extra:
        with open(args.extra, "r", encoding="utf-8") as fh:
            for k, v in json.load(fh).items():
                extra[k] = {**extra.get(k, {}), **v}

    m = scan_dir_to_manifest(
        args.dir, args.base_url, args.version,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), extra=extra,
    )
    if args.prune_to:
        m.assets = sorted(m.assets, key=lambda a: (a.tier, a.size_bytes))[: args.prune_to]

    errs = m.validate()
    if errs:
        print("manifest 校验失败：", file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        return 2
    if not args.skip_url_check:
        for e in check_urls(m):
            print("  ! " + e, file=sys.stderr)

    payload = m.to_json()
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
    for dest in args.copy_to:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        shutil.copyfile(args.out, dest)
        print(f"  复制 -> {dest}")

    print(f"OK {args.out}  version={args.version}")
    for a in m.assets:
        print(f"  [{a.tier}] {a.id:28s} {fmt_size(a.size_bytes):>9s}  {a.quant:8s} "
              f"ram>={fmt_size(a.min_ram_bytes)} free>={fmt_size(a.min_free_bytes)}")
    print("提示：把每个 asset 的 .gguf 也传到 --base-url 指向的位置，并把 LICENSE.txt/NOTICE 打进 App 的开源许可页")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
