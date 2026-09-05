#!/usr/bin/env bash
# 一条命令：把 App 编译 + 签名 + 装进你的 iPhone 15 并启动。
# 你不需要打开 Xcode 界面，但需要：一台 Mac（装了 Xcode 16+）+ 你的 Apple ID（免费个人账号即可）+ 数据线。
#
#   ./install-on-iphone15.sh                 # 只装"系统翻译"版（4MB，秒装，先验链路）
#   ./install-on-iphone15.sh --model both    # 把 462MB + 1133MB 两个档都打进 App（离线零额外操作）
#   ./install-on-iphone15.sh --model small   # 只打 462MB 档
#   ./install-on-iphone15.sh --dry-run       # 只打印将要执行的命令（可在任何机器上预演）
#
# 为什么"零额外操作"必须把模型打进包：运行时下载需要 CDN + 断点续传 + 校验 UI，
# 那是阶段 2 的工程；自用测试就一个原则：装上就翻。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IOS="$ROOT/ios"
MODEL_MODE="none"          # none | small | big | both
TEAM="${DEVELOPMENT_TEAM:-}"
DEVICE=""
BUNDLE_ID="${BUNDLE_ID:-dev.localmt.app}"
HF_BASE="${HF_BASE:-https://huggingface.co}"   # 大陆直连慢/断：HF_BASE=https://hf-mirror.com 重跑即可（体积比对是宽松的）

# 实测过的直链（我读过它们的 GGUF 头：general.architecture=hunyuan-dense）
SMALL_URL="$HF_BASE/tencent/Hy-MT2-1.8B-1.25Bit-GGUF/resolve/main/Hy-MT2-1.8B-1.25Bit.gguf"
SMALL_SIZE=462000000
BIG_URL="$HF_BASE/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf"
BIG_SIZE=1133000000

say()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

RUN=()
DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL_MODE="${2:-both}"; shift 2 ;;
    --model=*) MODEL_MODE="${1#*=}"; shift ;;
    --team) TEAM="$2"; shift 2 ;;
    --device) DEVICE="$2"; shift 2 ;;
    --bundle-id) BUNDLE_ID="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1（--help 看用法）" ;;
  esac
done

do_() { if [[ $DRY == 1 ]]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }

# ---------------------------------------------------------------- 环境自检
preflight() {
  say "环境自检"
  if [[ "$(uname -s)" != "Darwin" ]]; then
    if [[ $DRY == 1 ]]; then warn "非 macOS：dry-run 预演继续（真跑必须在 Mac 上）"; return 0; fi
    die "这一步必须在 macOS 上跑（Linux 不能给 iOS 签名/编译）"
  fi
  if command -v xcodebuild >/dev/null; then xcodebuild -version | head -1
  elif [[ $DRY != 1 ]]; then die "没找到 xcodebuild：先 xcode-select --install 或装 Xcode 16+"; fi
  command -v xcodegen >/dev/null || warn "缺 xcodegen → brew install xcodegen"
  # tools/build_llama_ios.sh 需要这三个（Vendor 已存在时可以不装）
  if [[ ! -d "$IOS/Vendor/llama.xcframework" ]]; then
    for t in git cmake ninja; do command -v "$t" >/dev/null || warn "缺 $t → brew install $t"; done
  fi
  [[ "${HF_BASE:-}" == *hf-mirror* ]] && echo "  用镜像下载权重：$HF_BASE"
  command -v curl >/dev/null || die "缺 curl"
  if [[ -z "$TEAM" ]]; then
    TEAM="$(security find-identity -v -p codesigning 2>/dev/null | grep -oE '\(([A-Z0-9]{10})\)' | head -1 | tr -d '()' || true)"
  fi
  if [[ -z "$TEAM" ]]; then
    warn "没探测到 DEVELOPMENT_TEAM。请在 Xcode → Settings → Accounts 登录你的 Apple ID，"
    warn "或手动指定：--team XXXXXXXXXX  （个人免费账号也有 Team ID，就是那 10 位）"
  else
    echo "  Team: $TEAM"
  fi
}

list_devices() {
  say "已连接设备"
  xcrun devicectl list devices 2>/dev/null | sed -n '1,12p' || \
    die "看不到设备：插线 → iPhone 上点“信任此电脑”→ 重试"
}

# ---------------------------------------------------------------- 权重打包
fetch_model() {   # $1=url $2=目标文件 $3=预期体积（宽松比对）
  local url="$1" dst="$2" expect="$3"
  mkdir -p "$(dirname "$dst")"
  if [[ -f "$dst" ]]; then
    local have; have="$(stat -f%z "$dst" 2>/dev/null || stat -c%s "$dst")"
    if [[ "$have" == "$expect" ]] || (( ${have:-0} > expect * 9 / 10 )); then
      echo "  已存在 $(basename "$dst")（$have 字节）→ 跳过下载"
      return 0
    fi
    warn "$(basename "$dst") 体积异常（$have），重下"
    rm -f "$dst"
  fi
  say "下载 $(basename "$dst")（约 $((expect / 1000000)) MB）"
  do_ "curl -fL --retry 3 --retry-delay 3 -C - -o '$dst' '$url' --progress-bar"
}

make_manifest() {
  say "生成 manifest.json（sha256 实测，绝不手填）"
  do_ "cd '$ROOT' && python3 tools/build_manifest.py --dir '$IOS/Resources/models' \
        --base-url 'bundle://models' --version \"bundle-$(date +%Y.%m.%d)\" \
        --out '$IOS/Resources/manifest.json' --skip-url-check"
  [[ $DRY == 1 ]] || cat "$IOS/Resources/manifest.json" | head -20
}

# ---------------------------------------------------------------- 构建 + 装机
build() {
  say "生成工程并编译（arm64 真机）"
  # ── 引擎框架：project.yml 里 `- framework: Vendor/llama.xcframework` 必须有实物，
  #    否则 xcodegen 之后 xcodebuild 报 "framework not found"。首次约 6 分钟（编两遍 arm64）。
  if [[ -d "$IOS/Vendor/llama.xcframework" ]]; then
    say "复用 ios/Vendor/llama.xcframework（换 llama 版本：rm -rf 它再跑）"
  elif [[ $DRY == 1 ]]; then
    warn "[dry-run] 会执行 ./tools/build_llama_ios.sh"
  else
    say "构建 llama.xcframework（v0.4.0 起，含 Metal）"
    ( cd "$ROOT" && ./tools/build_llama_ios.sh ) || die "llama 框架构建失败：先看上面 cmake/ninja 的报错"
  fi

  do_ "cd '$IOS' && xcodegen generate"

  # 模型进 bundle 就靠 Resources/models/*.gguf 本身（iOS 只拷贝不压缩；不用 ODR，
  # 因为 ODR 会引入"首次要联网下载"，与"零额外操作"冲突）。代码侧用运行时探测。
  [[ "$MODEL_MODE" != "none" && $DRY != 1 ]] && {   # none = 只出壳（走系统 Translation 兜底）
     ls -lh "$IOS/Resources/models" 2>/dev/null || die "Resources/models 是空的：模型没下进来"
     du -sh "$IOS/Resources/models" 2>/dev/null || true
  }

  do_ "cd '$IOS' && xcodebuild -project LocalMT.xcodeproj -scheme LocalMT \
        -configuration Debug -destination 'generic/platform=iOS' \
        -derivedDataPath dd -allowProvisioningUpdates \
        CODE_SIGN_STYLE=Automatic DEVELOPMENT_TEAM='${TEAM}' \
        PRODUCT_BUNDLE_IDENTIFIER='${BUNDLE_ID}' build"
}

install() {
  local app="$IOS/dd/Build/Products/Debug-iphoneos/LocalMT.app"
  [[ $DRY == 1 || -d "$app" ]] || die "没找到编译产物 $app（上一步编译失败？看上面的 xcodebuild 输出）"

  if [[ -z "$DEVICE" && $DRY == 1 ]]; then DEVICE="<UDID>"; fi
  if [[ -z "$DEVICE" ]]; then
    DEVICE="$(xcrun devicectl list devices 2>/dev/null | awk '/iPhone/{print $NF; exit}' | tr -d ')' || true)"
    [[ "$DEVICE" =~ ^[0-9A-Fa-f-]{20,}$ ]] || { list_devices; die "请用 --device <UDID> 指定设备"; }
  fi

  say "安装到设备 $DEVICE"
  do_ "xcrun devicectl device install app --device '$DEVICE' '$app'"
  say "启动"
  do_ "xcrun devicectl device process launch --device '$DEVICE' '$BUNDLE_ID' || true"
  cat <<'TIP'

  如果启动即闪退 / 提示"未受信任的开发者"：
    iPhone → 设置 → 通用 → VPN 与设备管理 → Developer App → 点“信任”
    （装完 1 分钟内必须做这件事，否则进程会被系统立刻杀掉）
  7 天后过期：重跑本脚本即可（免费个人账号上限 7 天、同时 3 个自签 App）。
TIP
}

# ---------------------------------------------------------------- main
case "$MODEL_MODE" in
  none) : ;;
  small) NEED_SMALL=1; NEED_BIG=0 ;;
  big)   NEED_SMALL=0; NEED_BIG=1 ;;
  both)  NEED_SMALL=1; NEED_BIG=1 ;;
  *) die "--model 只能是 none|small|big|both" ;;
esac

preflight
if [[ $DRY != 1 ]]; then list_devices; else warn "dry-run：跳过设备枚举"; fi

if [[ "${NEED_SMALL:-0}" == 1 ]]; then
  fetch_model "$SMALL_URL" "$IOS/Resources/models/Hy-MT2-1.8B-1.25Bit.gguf" "$SMALL_SIZE"
fi
if [[ "${NEED_BIG:-0}" == 1 ]]; then
  fetch_model "$BIG_URL" "$IOS/Resources/models/Hy-MT2-1.8B-Q4_K_M.gguf" "$BIG_SIZE"
fi
if [[ "$MODEL_MODE" != "none" ]]; then
  warn "注意：iPhone 15（6GB）加载 1133MB 档时，'Increased Memory Limit' 这个 entitlement"
  warn "需要付费开发者账号才能生效；免费账号下请优先用 --model small 测试，或把 n_ctx 降到 1024。"
  make_manifest
fi

build
install

say "完成。上机后 4 个验收动作（详见 ios/BUILD-ON-YOUR-MAC.md）"
cat <<'CHK'
  1. 开飞行模式，翻译一遍 —— 这是"离线"的唯一硬证据
  2. 设置→开发者→Memory 或 Instruments 记峰值 RSS（目标 ≤1.1GB）
  3. 连翻 20 句，看第 15 句后是否明显变慢（热降频）
  4. small(462MB) vs big(1133MB) 各测 docs 里那 6 句，把 tokens/s 记下来贴给我
CHK
