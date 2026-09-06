"""模型清单（manifest）与选包逻辑 —— 这是 iOS / Android / CI 三方共享的唯一契约。

设计原则
  * App 包内**不放**模型权重（首包必须小），只放 manifest；权重由 App 自己下载。
  * 每个 asset 必须有 sha256：HuggingFace 的 URL 会被 CDN 截断/换文件，不做校验的下载在
    手机上等于随机崩溃。
  * 选包不靠"机型名"，靠 (物理内存, 可用磁盘, 是否允许下载大文件) 三元组。

对应文档: docs/05-模型分发与更新.md
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Optional

MANIFEST_SCHEMA_VERSION = 1

# 单位: 字节。用于把 "462 MB" 这类人写的值规范化。
_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3}


def parse_size(text: str) -> int:
    """'462MB' / '1.1 GB' / '462' -> bytes（十进制，与 HuggingFace 展示口径一致）。"""
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([KMGT]?B?)\s*", text.upper().replace(" ", " "))
    if not m:
        raise ValueError(f"无法解析体积: {text!r}")
    num, unit = float(m.group(1)), (m.group(2) or "B")
    if unit not in _UNITS:
        raise ValueError(f"不支持的单位: {unit}")
    return int(num * _UNITS[unit])


def fmt_size(nbytes: int) -> str:
    val, unit = float(nbytes), "B"
    for u in ("KB", "MB", "GB", "TB"):
        if abs(val) >= 1000:
            val, unit = val / 1000, u
        else:
            break
    return f"{val:.0f} {unit}" if abs(val) >= 10 else f"{val:.1f} {unit}"


@dataclass(frozen=True)
class ModelAsset:
    """一个可下载的权重文件（或一个逻辑上的"包"）。"""

    id: str                    # 例: "hy-mt2-1.8b-1.25bit"
    url: str                   # 必须是**自有 CDN**，见 docs/05
    sha256: str
    size_bytes: int
    quant: str = "1.25bit"     # gguf 里的 quant 名，仅用于展示与降级决策
    params_b: float = 1.8
    # 需要的运行条件（低于此值就不要选这个包）
    min_ram_bytes: int = 3 * 1000**3
    min_free_bytes: int = 1 * 1000**3
    min_android_api: int = 28
    # 能力位：编排层据此决定功能开关
    supports: tuple[str, ...] = ("text", "terminology", "style")
    languages: tuple[str, ...] = ()      # ISO-639-1；空 = 未知，按 ["zh","en"] 处理
    engine: str = "llama-cpp"            # llama-cpp | litert-lm | mnn | apple | mlkit
    license: str = "Apache-2.0"
    notes: str = ""
    # 下载优先级：小/先用的排前面
    tier: int = 1

    def is_usable_on(self, ram_bytes: int, free_bytes: int) -> bool:
        return ram_bytes >= self.min_ram_bytes and free_bytes >= self.min_free_bytes

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["supports"] = list(self.supports)
        d["languages"] = list(self.languages)
        return d


@dataclass
class Manifest:
    version: str
    updated_at: str                       # ISO-8601 UTC
    assets: list[ModelAsset] = field(default_factory=list)
    glossary_schema: int = 1              # 术语表格式版本，改了要能识别
    min_app_build: int = 1                # 低于此 build 强制升级 App（格式不兼容时用）
    forced_update: bool = False

    # ---------- 加载 ----------
    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Manifest":
        known = {f.name for f in ModelAsset.__dataclass_fields__.values()}
        assets = []
        for item in raw.get("assets", []):
            extra = set(item) - known
            if extra:
                raise ValueError(f"manifest asset 含未知字段 {sorted(extra)}（字段名写错会静默丢配置）")
            item = dict(item)
            for key in ("supports", "languages"):
                if key in item:
                    item[key] = tuple(item[key])
            assets.append(ModelAsset(**item))
        return cls(
            version=str(raw["version"]),
            updated_at=str(raw.get("updated_at", "")),
            assets=assets,
            glossary_schema=int(raw.get("glossary_schema", 1)),
            min_app_build=int(raw.get("min_app_build", 1)),
            forced_update=bool(raw.get("forced_update", False)),
        )

    @classmethod
    def load(cls, path: str) -> "Manifest":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(
            {
                "schema": MANIFEST_SCHEMA_VERSION,
                "version": self.version,
                "updated_at": self.updated_at,
                "glossary_schema": self.glossary_schema,
                "min_app_build": self.min_app_build,
                "forced_update": self.forced_update,
                "assets": [a.to_json() for a in self.assets],
            },
            ensure_ascii=False,
            indent=indent,
        )

    def by_id(self, asset_id: str) -> Optional[ModelAsset]:
        for a in self.assets:
            if a.id == asset_id:
                return a
        return None

    # ---------- 校验（发布前必跑，CI 里也跑） ----------
    def validate(self) -> list[str]:
        errs: list[str] = []
        seen: set[str] = set()
        if not self.assets:
            errs.append("manifest 没有任何 asset")
        for a in self.assets:
            if a.id in seen:
                errs.append(f"asset id 重复: {a.id}")
            seen.add(a.id)
            if not re.fullmatch(r"[0-9a-f]{64}", a.sha256 or ""):
                errs.append(f"{a.id}: sha256 不是 64 位小写十六进制（发布前请用 tools/build_manifest.py 生成）")
            if a.size_bytes <= 1_000_000:
                errs.append(f"{a.id}: size_bytes={a.size_bytes} 小得可疑（单位写错？）")
            if not (a.url.startswith("https://") or a.url.startswith("bundle://")):
                errs.append(f"{a.id}: url 必须是 https 或 bundle://（离线内置用 bundle://）")
            if a.size_bytes > 0 and a.min_free_bytes < int(a.size_bytes * 1.05):
                # 允许 1.05x 余量：解压/临时文件需要空间
                errs.append(f"{a.id}: min_free_bytes({a.min_free_bytes}) < size_bytes({a.size_bytes})，下载中途会满盘")
            if "zh" not in a.languages or "en" not in a.languages:
                errs.append(f"{a.id}: languages 未同时包含 zh/en（本产品主场景）")
        return errs

    # ---------- 选包 ----------
    def select_for_device(
        self,
        ram_bytes: int,
        free_bytes: int,
        api_level: Optional[int] = None,
        prefer: str = "quality",          # "footprint" | "quality"
    ) -> Optional[ModelAsset]:
        """按设备能力挑一个能跑的包。

        quality  : 能跑大的就跑大的（质量优先），只在装不下时降级。
        footprint: 同 tier 内选体积最小的（低端机/流量敏感）。
        """
        cands: list[ModelAsset] = []
        for a in self.assets:
            if not a.is_usable_on(ram_bytes, free_bytes):
                continue
            if api_level is not None and api_level < a.min_android_api:
                continue
            cands.append(a)
        if not cands:
            return None
        if prefer == "footprint":
            return sorted(cands, key=lambda a: (a.tier, a.size_bytes, -a.params_b))[0]
        # 质量优先：tier 小的优先，其次 params 大，最后体积大
        return sorted(cands, key=lambda a: (a.tier, -a.params_b, -a.size_bytes))[0]

    def downgrade_chain(self, asset: ModelAsset) -> list[ModelAsset]:
        """从给定包开始的降级顺序（内存告警时逐个尝试）。"""
        rest = [a for a in self.assets if a.id != asset.id]
        return [asset] + sorted(rest, key=lambda a: a.size_bytes)


def sha256_of_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def scan_dir_to_manifest(
    directory: str,
    base_url: str,
    version: str,
    updated_at: str,
    *,
    quant_by_name: Iterable[tuple[str, str]] = (
        ("1.25", "1.25bit"),
        ("2bit", "2bit"),
        ("q4", "Q4_K_M"),
        ("q5", "Q5_K_M"),
        ("q6", "Q6_K"),
        ("q8", "Q8_0"),
    ),
    extra: Optional[dict[str, dict[str, Any]]] = None,
) -> Manifest:
    """把一个本地权重目录扫成 manifest（sha256/size 全部实测，不手填）。

    extra: {asset_id: {min_ram_bytes: ..., languages: [...]}}，用于补上无法自动推断的字段。
    """
    extra = extra or {}
    assets: list[ModelAsset] = []
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".gguf", ".task", ".litertlm", ".mnn")):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        stem = os.path.splitext(name)[0]
        low = stem.lower()
        quant = next((q for key, q in quant_by_name if key in low), "raw")
        params = 1.8
        pm = re.search(r"([0-9]+(?:\.[0-9]+)?)b", low)
        if pm:
            params = float(pm.group(1))
        size = os.path.getsize(path)
        payload = {
            "id": stem,
            "url": f"{base_url.rstrip('/')}/{name}",
            "sha256": sha256_of_file(path),
            "size_bytes": size,
            "quant": quant,
            "params_b": params,
            # 默认：权重 × 1.35（KV cache + 运行时）作为内存门槛的保守值
            "min_ram_bytes": int(size * 1.35) + 512 * 1000**2,
            "min_free_bytes": int(size * 1.15),
        }
        payload.update(extra.get(stem, {}))
        assets.append(ModelAsset(**payload))
    return Manifest(version=version, updated_at=updated_at, assets=assets)
