#!/usr/bin/env bash
# 一条命令产出可直接安装的 APK：模型内置、debug 自签、装完就能离线翻译（不需要任何账号/商店）。
#   ./build-apk.sh              # 只系统兜底版（小，先验链路）
#   ./build-apk.sh --model both # 内置 462MB + 1133MB 两个档，App 内可切换
#   ./build-apk.sh --install    # 顺带 adb install 到连着的那台安卓机
# 依赖：JDK 17、Android SDK（含 build-tools/platform-tools）、NDK r27、cmake。CI 版见 .github/workflows/android-apk.yml
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AND="$ROOT/android"
MODEL_MODE="none"; DO_INSTALL=0; WITH_NATIVE=1
HF="https://huggingface.co"
SMALL_URL="$HF/tencent/Hy-MT2-1.8B-1.25Bit-GGUF/resolve/main/Hy-MT2-1.8B-1.25Bit.gguf"
BIG_URL="$HF/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf"
say(){ printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }; warn(){ printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do case "$1" in
  --model) MODEL_MODE="${2:-both}"; shift 2;;  --model=*) MODEL_MODE="${1#*=}"; shift;;
  --install) DO_INSTALL=1; shift;;  --no-native) WITH_NATIVE=0; shift;;
  *) die "未知参数 $1";; esac; done

: "${ANDROID_HOME:=${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[[ -d "$ANDROID_HOME" ]] || die "没找到 ANDROID_HOME=$ANDROID_HOME（装 Android Studio 或设环境变量）"
command -v java >/dev/null || die "缺 JDK 17"
java -version 2>&1 | head -1 | grep -qE '"(1[7-9]|2[0-9])' || warn "JDK 版本偏旧，AGP 8 需要 17+"

fetch(){ local url="$1" dst="$2"; mkdir -p "$(dirname "$dst")"
  [[ -f "$dst" && $(stat -c%s "$dst" 2>/dev/null || stat -f%z "$dst") -gt 1000000 ]] && { echo "  已存在 $(basename "$dst")"; return; }
  say "下载 $(basename "$dst")"; curl -fL --retry 3 -C - -o "$dst" "$url" --progress-bar; }

if [[ "$MODEL_MODE" != "none" ]]; then
  mkdir -p "$AND/app/src/main/assets/models"
  [[ "$MODEL_MODE" =~ (small|both) ]] && fetch "$SMALL_URL" "$AND/app/src/main/assets/models/Hy-MT2-1.8B-1.25Bit.gguf"
  [[ "$MODEL_MODE" =~ (big|both)   ]] && fetch "$BIG_URL"   "$AND/app/src/main/assets/models/Hy-MT2-1.8B-Q4_K_M.gguf"
  ( cd "$ROOT" && python3 tools/build_manifest.py --dir "$AND/app/src/main/assets/models" \
      --base-url 'file:///android_asset/models' --version "bundle-$(date +%Y.%m.%d)" \
      --out "$AND/app/src/main/assets/manifest.json" --skip-url-check )
fi

if [[ $WITH_NATIVE == 1 ]]; then
  say "编译 llama.cpp 的 arm64 .so（16KB 页对齐，缺这个新机会 dlopen 失败）"
  NDK="$(ls -d "$ANDROID_HOME"/ndk/* 2>/dev/null | sort -V | tail -1)"
  [[ -n "$NDK" ]] || { warn "没装 NDK：$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager 'ndk;27.2.12479018'"; WITH_NATIVE=0; }
  if [[ $WITH_NATIVE == 1 ]]; then
    command -v cmake >/dev/null || die "缺 cmake（brew install cmake / apt install cmake）"
    V="${LLAMA_VERSION:-v0.4.0}"   # 实测 GitHub releases/latest = v0.4.0（2026-09-04）
    [[ -d "$ROOT/vendor/llama.cpp" ]] || git clone --depth 1 --branch "$V" https://github.com/ggml-org/llama.cpp "$ROOT/vendor/llama.cpp"
    TOOL="$NDK/toolchains/llvm/prebuilt/$(uname -s | tr A-Z a-z)$(uname -m | sed 's/x86_64/-x86_64/;s/arm64/-aarch64/')/bin"
    cmake -S "$ROOT/vendor/llama.cpp" -B "$ROOT/vendor/llama.cpp/build-and" -G Ninja \
      -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
      -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-28 -DANDROID_STL=c++_static \
      -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
      -DLLAMA_BUILD_APP=OFF -DLLAMA_OPENSSL=OFF -DLLAMA_CURL=OFF -DLLAMA_SO=ON \
      -DCMAKE_C_FLAGS="-march=armv8.2-a+dotprod+fp16" \
      -DCMAKE_SHARED_LINKER_FLAGS="-Wl,-z,max-page-size=16384"
    cmake --build "$ROOT/vendor/llama.cpp/build-and" -j "$(nproc)"
    mkdir -p "$AND/app/src/main/jniLibs/arm64-v8a"
    cp "$ROOT"/vendor/llama.cpp/build-and/*/*.so "$AND/app/src/main/jniLibs/arm64-v8a/" 2>/dev/null || \
      cp "$ROOT"/vendor/llama.cpp/build-and/*.so "$AND/app/src/main/jniLibs/arm64-v8a/"
    ls -la "$AND/app/src/main/jniLibs/arm64-v8a"
  fi
fi

say "打包 APK"
cd "$AND"
[[ -x ./gradlew ]] || die "这里需要 Gradle 工程（首次用 Android Studio 打开 android/ 生成 wrapper，或直接跑 CI）"
./gradlew :app:assembleDebug
APK="$(ls -t app/build/outputs/apk/debug/*.apk | head -1)"
say "产物：$APK （$(du -h "$APK" | cut -f1)）"
python3 "$ROOT/tools/lint_platform_code.py" --root "$ROOT" || warn "静态自查有告警，见上"
if [[ $DO_INSTALL == 1 ]]; then
  say "adb install"
  "$ANDROID_HOME/platform-tools/adb" install -r -g "$APK"
  "$ANDROID_HOME/platform-tools/adb" shell am start -n dev.localmt/.MainActivity
  echo "装好了。手机上先做：开飞行模式翻一句，确认是本地。"
else
  echo "安装：adb install -r -g '$APK'   （或直接把 APK 传到手机，允许“未知来源”）"
fi
warn "1.6GB 级 APK 只能侧载分发；上 Play 有包体/分发限制，需按 docs/05 改成运行时下载。"
