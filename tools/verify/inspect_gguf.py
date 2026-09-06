#!/usr/bin/env python3
"""GGUF 元数据检查器：不加载权重，只读文件头/元数据/张量信息段。

用途：飞行模式无译文的第一步定位（见 handoff/FLIGHT_DEBUG.md）。
在 Mac 上对下载好的 .gguf 运行，回答三个问题：
  1. general.architecture 是否被 llama.cpp 支持（hunyuan-dense ✓）
  2. 张量量化类型是否在 stock llama.cpp 的 ggml 类型表内
     （1.25Bit 这类极端量化若映射到 UNKNOWN(id)，stock v0.4.0 大概率加载失败——
      那就是无译文的根因，与 iOS 无关）
  3. tokenizer.chat_template 是否真的缺失（我们模板自带的依据核验）

用法：python3 tools/verify/inspect_gguf.py <model.gguf> [--tensors]
自测：python3 tools/verify/inspect_gguf.py --selftest
"""
from __future__ import annotations

import struct
import sys
import io

GGUF_MAGIC = 0x46554747  # "GGUF" little-endian

GGML_TYPE_NAMES = {
    0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 6: "Q5_0", 7: "Q5_1",
    8: "Q8_0", 9: "Q8_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
    14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
    19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS",
    24: "IQ1_S", 25: "IQ4_NL", 26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S",
    29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
    36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4",
    # 注：枚举值以目标版本 ggml.h 为准；对不上的 id 会显式报 UNKNOWN，
    # 恰恰是最需要人眼判断的信号，而不是本脚本的 bug。
}

T_UINT8, T_INT8, T_UINT16, T_INT16, T_UINT32, T_INT32 = 0, 1, 2, 3, 4, 5
T_FLOAT32, T_BOOL, T_STRING, T_ARRAY, T_UINT64, T_INT64, T_FLOAT64 = 6, 7, 8, 9, 10, 11, 12


class R:
    def __init__(self, f: io.BufferedIOBase):
        self.f = f

    def _u(self, n: int, signed: bool = False):
        return int.from_bytes(self.f.read(n), "little", signed=signed)

    def u8(self): return self._u(1)
    def i8(self): return self._u(1, True)
    def u16(self): return self._u(2)
    def i16(self): return self._u(2, True)
    def u32(self): return self._u(4)
    def i32(self): return self._u(4, True)
    def u64(self): return self._u(8)
    def i64(self): return self._u(8, True)
    def f32(self): return struct.unpack("<f", self.f.read(4))[0]
    def f64(self): return struct.unpack("<d", self.f.read(8))[0]

    def string(self) -> str:
        n = self.u64()
        return self.f.read(n).decode("utf-8", errors="replace")

    def value(self, t: int):
        return {
            T_UINT8: self.u8, T_INT8: self.i8, T_UINT16: self.u16, T_INT16: self.i16,
            T_UINT32: self.u32, T_INT32: self.i32, T_UINT64: self.u64, T_INT64: self.i64,
            T_FLOAT32: self.f32, T_FLOAT64: self.f64, T_BOOL: lambda: bool(self.u8()),
            T_STRING: self.string,
        }.get(t, None)()

    def any_value(self):
        t = self.u32()
        if t == T_ARRAY:
            et = self.u32()
            n = self.u64()
            return [self.value(et) for _ in range(n)] if et != T_STRING else [self.string() for _ in range(n)]
        return self.value(t)


def inspect(path: str, show_tensors: bool = False):
    with open(path, "rb") as f:
        r = R(f)
        magic = r.u32()
        if magic != GGUF_MAGIC:
            raise SystemExit(f"✗ 不是 GGUF 文件（magic={magic:#x}）——先怀疑下载损坏")
        version = r.u32()
        n_tensors = r.u64()
        n_kv = r.u64()
        meta = {}
        for _ in range(n_kv):
            k = r.string()
            meta[k] = r.any_value()
        tensors = []
        for _ in range(n_tensors):
            name = r.string()
            n_dims = r.u32()
            dims = [r.u64() for _ in range(n_dims)]
            ttype = r.u32()
            r.u64()  # offset
            tensors.append((name, ttype, dims))

        print(f"文件: {path}")
        print(f"gguf version: {version}   tensors: {n_tensors}   meta kv: {n_kv}")
        arch = meta.get("general.architecture")
        print(f"general.architecture : {arch}")
        print(f"general.file_type    : {meta.get('general.file_type')}")
        print(f"context_length       : {meta.get(f'{arch}.context_length')}")
        print(f"tokenizer.chat_template 存在: {'tokenizer.chat_template' in meta}"
              f"（缺失=模板必须宿主自带，本项目已内置，见 LlamaBridge.mm）")
        for k in ("tokenizer.ggml.bos_token_id", "tokenizer.ggml.eos_token_id"):
            if k in meta:
                print(f"{k}: {meta[k]}")
        hist: dict[str, int] = {}
        unknown: dict[str, int] = {}
        for name, ttype, _ in tensors:
            nm = GGML_TYPE_NAMES.get(ttype)
            if nm is None:
                unknown[f"id={ttype}"] = unknown.get(f"id={ttype}", 0) + 1
                nm = f"UNKNOWN({ttype})"
            hist[nm] = hist.get(nm, 0) + 1
        print("张量量化分布:", dict(sorted(hist.items(), key=lambda x: -x[1])))
        if unknown:
            print(f"⚠️  未知张量类型 {unknown} —— stock llama.cpp v0.4.0 大概率拒绝加载；")
            print("    这将直接表现为「加载失败」而非 iOS 特有问题（对照 FLIGHT_DEBUG.md 决策树）")
        elif tensors:
            print("✓ 全部张量类型均在 stock ggml 类型表内（加载兼容性必要条件，非充分条件）")
        if show_tensors:
            for name, ttype, dims in tensors[:40]:
                print(f"  {name:60s} {GGML_TYPE_NAMES.get(ttype, f'UNKNOWN({ttype})'):10s} {dims}")


def selftest():
    """手工构造一个最小合法 GGUF（含 1 个元数据 + 2 张量）验证解析器。"""
    buf = io.BytesIO()

    def s(x: str):
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    meta = (s("general.architecture") + struct.pack("<I", T_STRING) + s("hunyuan-dense")
            + s("general.file_type") + struct.pack("<I", T_UINT32) + struct.pack("<I", 99))
    tensor = (s("token_embd.weight") + struct.pack("<I", 2)
              + struct.pack("<Q", 4) + struct.pack("<Q", 8)   # dims
              + struct.pack("<I", 24) + struct.pack("<Q", 0)  # IQ1_S, offset
              + s("output.weight") + struct.pack("<I", 1)
              + struct.pack("<Q", 4)
              + struct.pack("<I", 999) + struct.pack("<Q", 0))  # 未知类型 id=999
    buf.write(struct.pack("<III", GGUF_MAGIC, 3, 0)[:4] + struct.pack("<I", 3))
    buf.write(struct.pack("<Q", 2))          # tensor count
    buf.write(struct.pack("<Q", 2))          # kv count
    buf.write(meta + tensor)
    path = "/tmp/selftest.gguf"
    open(path, "wb").write(buf.getvalue())
    inspect(path)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if "--selftest" in sys.argv:
        selftest()
    elif args:
        inspect(args[0], show_tensors="--tensors" in sys.argv)
    else:
        raise SystemExit(__doc__)
