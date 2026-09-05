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
xcrun --sdk iphoneos --show-sdk-path >/dev/null || { say "iphoneos SDK not found"; exit 1; }

if [ ! -d "$SRC/.git" ]; then
  say "clone llama.cpp $REF"
  mkdir -p "$(dirname "$SRC")"
  git clone --depth 1 --branch "$REF" https://github.com/ggml-org/llama.cpp "$SRC"
else
  say "reusing existing $SRC"
fi

build_one(){
  local sdk="$1"
  local tag="$2"
  local bdir="$ROOT/build/llama-$tag"
  say "building $tag (sdk=$sdk)"
  cmake -S "$SRC" -B "$bdir" -G Ninja \
    -DCMAKE_SYSTEM_NAME=iOS \
    -DCMAKE_OSX_SYSROOT="$sdk" \
    -DCMAKE_OSX_DEPLOYMENT_TARGET=18.0 \
    -DCMAKE_OSX_ARCHITECTURES=arm64 \
    -DBUILD_SHARED_LIBS=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF \
    -DGGML_METAL=ON \
    -DGGML_METAL_EMBED_LIBRARY=ON \
    -DGGML_LLAMAFILE=ON -DGGML_NATIVE=OFF \
    -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
    -DCMAKE_C_FLAGS="-O3" -DCMAKE_CXX_FLAGS="-O3"
  cmake --build "$bdir" --parallel "$(sysctl -n hw.ncpu)"
  mkdir -p "$bdir/out/Headers"
  cp "$SRC/include/llama.h" "$SRC/ggml/include/"*.h "$bdir/out/Headers/"
  # v0.4.0 uses lib*.a naming (libllama.a, libggml.a, libggml-base.a, libggml-cpu.a, libggml-metal.a)
  find "$bdir" -maxdepth 4 \( \
        -name 'libllama.a' -o -name 'libggml.a' \
        -o -name 'libggml-base.a' -o -name 'libggml-cpu.a' \
        -o -name 'libggml-metal.a' -o -name 'libllama-common-base.a' \
        \) -exec cp -f {} "$bdir/out/" \;
  ls -l "$bdir/out"
  echo "$bdir/out"
}

DEV_OUT=$(build_one iphoneos ios-arm64 | tail -1)
mkdir -p "$ROOT/ios/Vendor" && rm -rf "$OUT"
args=(-create-xcframework -library "$DEV_OUT/libllama.a" -headers "$DEV_OUT/Headers")
for l in libggml libggml-base libggml-cpu libggml-metal libllama-common-base; do
  [ -f "$DEV_OUT/$l.a" ] && args+=(-library "$DEV_OUT/$l.a" -headers "$DEV_OUT/Headers")
done
if [ $DEVICE_ONLY -eq 0 ]; then
  SIM_OUT=$(build_one iphonesimulator ios-arm64-simulator | tail -1)
  args+=(-library "$SIM_OUT/libllama.a" -headers "$SIM_OUT/Headers")
  for l in libggml libggml-base libggml-cpu libggml-metal libllama-common-base; do
    [ -f "$SIM_OUT/$l.a" ] && args+=(-library "$SIM_OUT/$l.a" -headers "$SIM_OUT/Headers")
  done
fi
xcodebuild "${args[@]}" -output "$OUT"

say "done: $(du -sh "$OUT" | cut -f1)  slices: $(ls "$OUT" | tr '\n' ' ')"
say "verify: nm must contain llama_model_load_from_file"
count=$(nm -gU "$OUT/ios-arm64/libllama.a" 2>/dev/null | grep -c llama_model_load_from_file || true)
if [ "${count:-0}" -gt 0 ]; then
  say "OK: found $count occurrence(s) of llama_model_load_from_file"
else
  say "WARNING: symbol not found, check llama.cpp version"
fi
echo
echo "next: ./ios/install-on-iphone15.sh --model small"
