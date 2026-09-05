#!/usr/bin/env bash
# 复现 Arena 对 llama.cpp API 的编译级核对（只需 git + g++/clang++，无需 Xcode）。
# 用法: ./tools/verify/verify_llama_api.sh [引用，默认 v0.4.0]
# 退出码：0 = 探针1通过且探针2按预期失败（API 漂移证据成立）
#         1 = 任一结果与预期不符（llama.cpp 又改了 API，改桥接代码前先来这里看）
set -euo pipefail
REF="${1:-v0.4.0}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CXX="${CXX:-g++}"
command -v "$CXX" >/dev/null || CXX=clang++

git clone --depth 1 --branch "$REF" https://github.com/ggml-org/llama.cpp "$TMP/llama.cpp" 1>&2
INC="-I $TMP/llama.cpp/include -I $TMP/llama.cpp/ggml/include"

echo "== [1/2] 补丁后 API 探针（预期：编译通过）=="
"$CXX" -std=gnu++17 -fsyntax-only -Wall -Wno-deprecated-declarations $INC \
    "$ROOT/tools/verify/probe_v040.cpp" \
  && echo "OK: LlamaBridge 现用写法与 $REF 头文件一致" \
  || { echo "✗ 桥接代码用了 $REF 里不存在的 API —— 先改 ios/Bridge/LlamaBridge.mm"; exit 1; }

echo "== [2/2] 旧写法对照探针（预期：编译失败）=="
if "$CXX" -std=gnu++17 -fsyntax-only $INC "$ROOT/tools/verify/probe_old.cpp" 2>"$TMP/err"; then
  echo "✗ 意外：旧写法竟能编译 —— $REF 的 API 与 v0.4.0 不同，结论需重新核对"; exit 1
fi
echo "符合预期：旧写法编译失败，错误摘要 ↓"
head -5 "$TMP/err"
echo "全部符合预期。"
