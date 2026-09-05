/* 免安装试用外壳（浏览器内推理）。
 * 目标只有一个：让你在没有 Mac、没有账号的情况下，10 分钟内在 iPhone 上"点一下就翻译"。
 * 三条硬限制要提前说清（都写进 UI）：
 *   1) iOS Safari 的 WebGPU + 内存预算跑不动 462MB 的 Hy-MT2 档 → 这里默认 600M 试玩模型；
 *   2) 首次要联网下模型（之后浏览器缓存命中即可离线）；
 *   3) 自学习在这里只能做"术语替换表"（NMT 没有 prompt 注入口），原生 App 才有 few-shot。
 */

const $ = (id) => document.getElementById(id);
const els = {
  dir: $("dir"), tier: $("tier"), custom: $("custom"), src: $("src"), out: $("out"),
  go: $("go"), swap: $("swap"), cp: $("cp"), learn: $("learn"), stat: $("stat"),
  prog: $("prog"), eng: $("eng"), install: $("install"),
};

const HF = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3";   // 版本上线前请改精确版本号
let pipe = null;
let modelId = els.tier.value;
let lastResult = "";
let deferredPrompt = null;

/* ---------------- 自学习：术语替换表（localStorage，永不出网） ---------------- */
const LKEY = "localmt.terms.v1";
const terms = () => { try { return JSON.parse(localStorage.getItem(LKEY) || "[]"); } catch { return []; } };
const saveTerms = (t) => localStorage.setItem(LKEY, JSON.stringify(t));

function guessDirection(text) {
  const cjk = [...text].filter((c) => /[㐀-鿿豈-﫿]/.test(c)).length;
  const latin = [...text].filter((c) => /[A-Za-z]/.test(c)).length;
  return cjk > 0 && cjk / Math.max(1, cjk + latin) >= 0.25 ? "zh2en" : "en2zh";
}
function resolveDir() {
  const d = els.dir.value === "auto" ? guessDirection(els.src.value.trim()) : els.dir.value;
  return d === "zh2en" ? { from: "zho_Hans", to: "eng_Latn" } : { from: "eng_Latn", to: "zho_Hans" };
}

/* 术语命中：给 NMT 的注入方式只能是"源文里替换成目标词" + "译文里强制回替"，
   所以 enforce 用后替换，hint 用预替换（两种都会改变模型看到的文本，注意副作用）。 */
function applyTermsPre(text) {
  let out = text;
  for (const t of terms().filter((t) => t.mode === "hint")) out = out.split(t.src).join(t.dst);
  return out;
}
function applyTermsPost(text) {
  let out = text, changed = [];
  for (const t of terms().filter((t) => t.mode === "enforce")) {
    if (t.dst && out.includes(t.src)) { out = out.split(t.src).join(t.dst); changed.push([t.src, t.dst]); }
  }
  return [out, changed];
}

/* ---------------- 模型加载 ---------------- */
async function load() {
  if (pipe) return pipe;
  els.stat.textContent = "加载推理库…";
  const mod = await import(HF);
  const { pipeline, env } = mod;
  env.allowLocalModels = false;
  env.useBrowserCache = true;                 // 二次进入走缓存 → 飞行模式可用
  els.prog.hidden = false;
  els.prog.firstElementChild.style.width = "12%";
  els.stat.textContent = "下载模型（一次性，之后离线可用）…";
  pipe = await pipeline("text2text-generation", modelId, {
    dtype: "q8",                              // iOS 上 fp16 更省内存但部分算子回落 CPU，q8 更稳
    progress_callback: (p) => {
      if (p?.status === "progress" && p.total) {
        els.prog.firstElementChild.style.width = `${Math.round((p.loaded / p.total) * 100)}%`;
      }
    },
  });
  els.prog.hidden = true;
  els.stat.textContent = "就绪（本地）";
  els.eng.innerHTML = `引擎：<b>transformers.js / WebGPU</b> · 模型 ${modelId} · ` +
    `GPU: ${await hasWebGPU() ? "已启用" : '<span class="warn">不可用，回落 CPU（很慢）</span>'}`;
  return pipe;
}
async function hasWebGPU() {
  try { return !!(navigator.gpu && await navigator.gpu.requestAdapter()); } catch { return false; }
}

/* ---------------- 翻译 ---------------- */
async function translate() {
  const text = els.src.value.trim();
  if (!text) { els.out.textContent = ""; return; }
  els.go.disabled = true;
  try {
    const p = await load();
    const { from, to } = resolveDir();
    const input = applyTermsPre(text);
    const t0 = performance.now();
    els.stat.textContent = "翻译中…";
    const res = await p(input, { max_new_tokens: Math.max(48, Math.min(512, input.length * 3)),
                                 src_lang: from, tgt_lang: to, num_beams: 1, do_sample: false });
    const ms = Math.round(performance.now() - t0);
    let out = (Array.isArray(res) ? res[0]?.generated_text : res?.generated_text || res) || "";
    out = String(out).replace(/^\s*(翻译|译文|Translation)[:：]\s*/i, "").trim();
    const [fixed, changed] = applyTermsPost(out);
    els.out.textContent = fixed || "（空）";
    lastResult = fixed;
    const tps = (input.length / Math.max(0.2, ms / 1000)).toFixed(1);
    els.stat.textContent = `本地 · ${ms}ms · ≈${tps} 字/s` + (changed.length ? ` · 术语已修正 ${changed.length}` : "");
    els.learn.hidden = false;
  } catch (e) {
    els.stat.textContent = "失败";
    els.out.innerHTML = `<span class="warn">${String(e?.message || e).replace(/[<>]/g, "")}</span><br>
      <small>常见原因：① 首次加载需要联网下模型 ② iOS Safari 内存不足（关掉其它标签再试）
      ③ 该模型不被 transformers.js 支持 —— 换一个 id 或直接用原生方案</small>`;
  } finally {
    els.go.disabled = false;
  }
}

/* ---------------- 交互 ---------------- */
let deb;
els.src.addEventListener("input", () => { clearTimeout(deb); deb = setTimeout(translate, 500); });
els.go.addEventListener("click", translate);
els.dir.addEventListener("change", translate);
els.swap.addEventListener("click", () => {
  const o = els.out.textContent; if (!o) return;
  els.src.value = o; els.out.textContent = ""; els.dir.value = els.dir.value === "zh2en" ? "en2zh" : "zh2en";
  translate();
});
els.cp.addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(els.out.textContent); els.stat.textContent = "已复制"; } catch {}
});
els.learn.addEventListener("click", () => {
  // 用户改了译文 → 从差异里取"最短公共替换"当作术语（粗糙版；原生 App 用 core/learn.py 的统计挖掘）
  const before = lastResult, after = els.out.textContent.trim();
  if (!after || before === after) { els.stat.textContent = "没有改动可学"; return; }
  const a = terms();
  for (const seg of after.split(/[\s,，。.]+/)) {
    if (seg.length >= 2 && !before.includes(seg) && a.length < 200) {
      a.push({ src: (before.match(/\S+/g) || []).find((w) => seg.includes(w)) || seg, dst: seg, mode: "enforce" });
      break;
    }
  }
  saveTerms(a);
  els.stat.textContent = `已记住 ${a.length} 条术语（本机保存，不出网）`;
});
els.tier.addEventListener("change", () => {
  const custom = els.tier.value === "__custom";
  els.custom.hidden = !custom;
  if (!custom) { modelId = els.tier.value; pipe = null; els.stat.textContent = "模型已切换，未加载"; }
});
els.custom.addEventListener("change", () => {
  const v = els.custom.value.trim(); if (v) { modelId = v; pipe = null; els.stat.textContent = "已填自定义模型"; }
});

/* ---------------- PWA：装到主屏幕 ---------------- */
if ("serviceWorker" in navigator && (location.protocol === "https:" || ["localhost", "127.0.0.1"].includes(location.hostname))) {
  navigator.serviceWorker.register("./sw.js").catch(() => els.eng.textContent = "SW 注册失败（需 HTTPS）");
}
addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredPrompt = e; els.install.hidden = false; });
els.install?.addEventListener("click", async () => {
  if (deferredPrompt) { deferredPrompt.prompt(); await deferredPrompt.userChoice; deferredPrompt = null; els.install.hidden = true; }
  else els.stat.textContent = "iOS 请手动：分享 → 添加到主屏幕";
});
