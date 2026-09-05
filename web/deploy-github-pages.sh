#!/usr/bin/env bash
# 免开发者账号的"零安装"路线：GitHub Pages 提供 HTTPS（Service Worker 必须 HTTPS），
# 手机浏览器打开 → 分享 → 添加到主屏幕，就有独立图标 + 离线外壳。
#   用法: ./deploy-github-pages.sh <你的GitHub用户名> <repo名>
set -euo pipefail
GH_USER="${1:?用法: ./deploy-github-pages.sh <用户名> <repo>}"
REPO="${2:?需要 repo 名}"
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v git >/dev/null || { echo "缺 git"; exit 1; }

# 没有图标就不装得像 App —— 用纯 stdlib 生成两个纯色 PNG（不依赖 Pillow/ImageMagick）
if [ ! -s "$HERE/icon-192.png" ]; then
  echo "! 生成 icon-192/512.png"
  python3 - "$HERE" <<'PY'
import os, struct, sys, zlib
d = sys.argv[1]
def png(path, size, rgba):
    raw = b"".join(b"\x00" + bytes(rgba) * size for _ in range(size))
    def chunk(t, data):
        body = t + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                 + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
for s in (192, 512):
    png(os.path.join(d, "icon-%d.png" % s), s, [27, 110, 243, 255])
print("  ok")
PY
fi

cd "$HERE"
[ -d .git ] || git init -q
git add -A
git commit -qm "localmt pwa shell" || true
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/$GH_USER/$REPO.git"
git push -uf origin main
cat <<EOT

下一步（网页操作，2 次点击）：
  1) 打开 https://github.com/$GH_USER/$REPO → Settings → Pages
     → Build and deployment: Source = Deploy from a branch → Branch: main /(root) → Save
  2) 约 1 分钟后 iPhone Safari 打开：
     https://$GH_USER.github.io/$REPO/
     → 分享 → 添加到主屏幕
     → 首次点"翻译"会自动下模型（约 250–600MB，视所选档），之后飞行模式也能用

三条提醒：
  * 必须 https。用 http/file:// 打开，Service Worker 不注册，离线能力=0。
  * 这条路线只能跑小模型（iOS Safari 的 WebGPU/内存预算），质量低于原生 462MB 档；
    用它评估"项目可行性"会得出错误结论。
  * 想加"自己的术语/历史"，数据就在 localStorage，清站点数据即抹掉；不发任何请求。
EOT
