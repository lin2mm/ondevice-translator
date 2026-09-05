"""ondevice-translator 共享核心（Python 参考实现 = 行为规格）。

iOS / Android 不直接用这些文件，但必须与 tests/ 里的用例保持一致：
测试就是跨语言的验收标准（CI 里用同一组 jsonl 用例对拍，见 docs/05）。
"""
from .engine import ChunkResult, Engine, Translation, Translator
from .glossary import Entry, Glossary, Mode
from .learn import Learner, TMHit
from .model_manifest import Manifest, ModelAsset, fmt_size, parse_size, scan_dir_to_manifest
from .prompt import TranslateRequest, builder_for
from .segmenter import estimate_tokens, pack, restore, split_sentences

__all__ = [
    "Translator", "Translation", "ChunkResult", "Engine",
    "Glossary", "Entry", "Mode", "Learner", "TMHit",
    "Manifest", "ModelAsset", "parse_size", "fmt_size", "scan_dir_to_manifest",
    "TranslateRequest", "builder_for",
    "split_sentences", "pack", "restore", "estimate_tokens",
]
