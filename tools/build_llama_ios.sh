#!/usr/bin/env bash
# Build llama.cpp into ios/Vendor/llama.xcframework (static libs + headers)
#
# Usage (from repo root):
#   ./tools/build_llama_ios.sh                # v0.4.0, arm64 device + simulator
#   LLAMA_REF=v0.3.0 ./tools/build_llama_ios.sh
#   ./tools/build_llama_ios.sh --device-only   # device only (faster)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
REF=${LLAMA_REF:-v0.4.0}
SRC=$ROOT/vendor/llama.cpp
OUT=$ROOT/ios/Vendor/llama.xcframework
DEVICE_ONLY=0
[ "${1:-}" = "--device-only" ] && DEVICE_ONLY=1

say(){ echo ""; echo "[llama-ios] $*"; }

command -v cmake >/dev/null || { say "missing cmake: brew install cmake ninja"; exit 1; }
command -v xcodebuild >/dev/null || { say "missing Xcode"; exit 1; }
command -v libtool >/dev/null || { say "missing libtool (Xcode command line tools)"; exit 1; }
xcrun --sdk iphoneos --show-sdk-path >/dev/null || { say "iphoneos SDK not found"; exit 1; }

if [ ! -d "$SRC/.git" ]; then
  say "clone llama.cpp $REF"
  mkdir -p "$(dirname "$SRC")"
  git clone --depth 1 --branch "$REF" https://github.com/ggml-org/llama.cpp "$SRC"
else
  say "reusing existing $SRC"
fi

say "=== Building llama.cpp ==="

build_arch(){
  local sdk="$1"        # iphoneos or iphonesimulator
  local tag="$2"        # arm64 or arm64-simulator
  local out_suffix="$3" # ios-arm64 or ios-arm64-simulator
  local bdir="$ROOT/build/llama-$tag"
  local outdir="$bdir/out"

  cmake -S "$SRC" -B "$bdir" -G Ninja \
    -DCMAKE_SYSTEM_NAME=iOS \
    -DCMAKE_OSX_SYSROOT="$sdk" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=18.0 \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DBUILD_SHARED_LIBS=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_APP=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_LLAMAFILE=ON -DGGML_NATIVE=OFF \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCMAKE_C_FLAGS="-O3" -DCMAKE_CXX_FLAGS="-O3"

  cmake --build "$bdir" --parallel "$(sysctl -n hw.ncpu)"
  mkdir -p "$outdir/Headers"
  cp "$SRC/include/llama.h" "$SRC/ggml/include/"*.h "$outdir/Headers/"
  # 幂等：排除上次的产物目录——否则重跑时 find 会把 out/libllama-all.a 拷到自身
  #（cp "same file" 报错，set -e 直接中断），libtool 的输入 glob 也会吞进上次产物。
  find "$bdir" -name "*.a" -type f -not -path "$outdir/*" -exec cp -f {} "$outdir/" \;
  ls -l "$outdir"
  echo "$outdir"
}

# Build device
DEV=$(build_arch iphoneos arm64 ios-arm64 | tail -1)

if [ $DEVICE_ONLY -eq 0 ]; then
  # Build simulator
  SIM=$(build_arch iphonesimulator arm64-simulator ios-arm64-simulator | tail -1)
fi

say "=== Creating xcframework ==="

mkdir -p "$ROOT/ios/Vendor"
rm -rf "$OUT"

if [ $DEVICE_ONLY -eq 1 ]; then
  # Device-only: merge all libs and create single-slice xcframework
  # 幂等：产物写 merged/ 子目录（与双 slice 路径一致）。"$DEV"/*.a 不递归子目录，
  # 不会把上次产物吞进输入；写在 $DEV 根上则重跑必炸（输出文件落进输入 glob）。
  mkdir -p "$DEV/merged"
  libtool -static -o "$DEV/merged/libllama-all.a" "$DEV"/*.a 2>&1 | grep -v "has no symbols" || true
  xcodebuild -create-xcframework \
    -library "$DEV/merged/libllama-all.a" -headers "$DEV/Headers" \
    -output "$OUT"
  rm -rf "$DEV/merged"
else
  # Device + Simulator: create xcframework with two slices
  # Note: we must use -static-library and specify libraries with their own headers
  mkdir -p "$DEV/merged" "$SIM/merged"
  libtool -static -o "$DEV/merged/libllama-all.a" "$DEV"/*.a 2>&1 | grep -v "has no symbols" || true
  libtool -static -o "$SIM/merged/libllama-all.a" "$SIM"/*.a 2>&1 | grep -v "has no symbols" || true

  xcodebuild -create-xcframework \
    -library "$DEV/merged/libllama-all.a" -headers "$DEV/Headers" \
    -library "$SIM/merged/libllama-all.a" -headers "$SIM/Headers" \
    -output "$OUT"

  rm -rf "$DEV/merged" "$SIM/merged"
fi

say "done: $(du -sh "$OUT" | cut -f1)  slices: $(ls "$OUT" | tr '\n' ' ')"
say "verify: nm must contain llama_model_load_from_file"
count=$(nm -gU "$OUT/ios-arm64/libllama-all.a" 2>/dev/null | grep -c llama_model_load_from_file || true)
if [ "${count:-0}" -gt 0 ]; then
  say "OK: found $count occurrence(s) of llama_model_load_from_file"
else
  say "WARNING: symbol not found, check llama.cpp version"
fi
echo
echo "next: ./ios/install-on-iphone15.sh --model small"
