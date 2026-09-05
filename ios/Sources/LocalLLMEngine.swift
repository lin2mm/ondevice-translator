import Foundation

/// A 档的"上层"：把 HyMTLlamaEngine（只会吐 token）组装成和 C 档同签名的 TranslateEngine。
/// 逻辑是 core/engine.py Translator 的移植（docs/03 第 4 节），不是新设计：
///   分句 → 打包(≤150 tok) → 模板 + 术语 + TM few-shot → 生成 → 校验/重试 → 术语兜底 → 拼接
/// 关键约束（docs/05）：失败必须抛错，绝不返回原文——返回原文会被 validate 判成 passthrough，UI 看起来"正常"。
final class LocalLLMTranslateEngine: TranslateEngine {
    private let llm: HyMTLlamaEngine
    private let glossary: Glossary
    private let tm: TranslationMemory?          // 自学习 L1；阶段 3 前允许为 nil
    private let maxChunkTokens = 150
    private let maxNewTokens = 512

    init(llm: HyMTLlamaEngine, glossary: Glossary = .shared, tm: TranslationMemory? = nil) {
        self.llm = llm
        self.glossary = glossary
        self.tm = tm
    }

    func translate(_ text: String, source: Locale.Language, target: Locale.Language) async throws -> String {
        let dir = LangPair.direction(source: source, target: target)
        var out: [String] = []
        for chunk in Chunker.pack(text, maxTokens: maxChunkTokens) {
            let prompt = buildPrompt(chunk, dir: dir)
            var s = OutputSanitizer.clean(try await run(prompt, .standard))
            if OutputSanitizer.looksLikeSource(s, chunk) {       // 复读原文 → 换保守采样重试一次
                s = OutputSanitizer.clean(try await run(prompt, .safe))
            }
            guard OutputSanitizer.acceptable(s, source: chunk, targetIsChinese: dir.isToChinese) else {
                throw EngineFailure.inner("本地模型输出未通过校验（疑似复读原文），已中止而不是返回原文")
            }
            out.append(glossary.apply(to: s, direction: dir))
        }
        return out.joined()
    }

    private func run(_ prompt: Prompt, _ sampling: SamplingParams) async throws -> String {
        try await llm.generate(prompt: prompt, maxNewTokens: maxNewTokens, sampling: sampling,
                               onToken: nil, shouldCancel: { false })
    }

    /// 术语表 + 已确认的 TM 例句进 prompt。不加 system prompt（模型卡明确不需要）。
    private func buildPrompt(_ chunk: String, dir: LangPair.Direction) -> Prompt {
        var user = ""
        let terms = glossary.hints(for: chunk, direction: dir)
        if !terms.isEmpty {
            user += "Terminology to follow:\n" + terms.map { "- \($0)" }.joined(separator: "\n") + "\n\n"
        }
        if let shots = tm?.fewShot(for: chunk, direction: dir, k: 3), !shots.isEmpty {
            user += "Reference translations:\n" + shots.map { "- \($0)" }.joined(separator: "\n") + "\n\n"
        }
        user += "Translate the following into \(dir.targetFullName). Output only the translation.\n\n\(chunk)"
        return Prompt(user: user)
    }
}

/// 分句 + 打包：与 core/segmenter.py 同一策略（标点闭合后切，超长句按 token 硬切）。
enum Chunker {
    static let enders: Set<Character> = ["。", "！", "？", "；", ".", "!", "?", ";", "\n"]

    static func sentences(_ text: String) -> [String] {
        var out: [String] = []
        var cur = ""
        for ch in text {
            cur.append(ch)
            if enders.contains(ch), cur.trimmingCharacters(in: .whitespacesAndNewlines).count >= 4 {
                out.append(cur); cur = ""
            }
        }
        if !cur.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { out.append(cur) }
        return out.isEmpty ? [text] : out
    }

    /// 粗算 token：中文 1 字 ≈ 1 tok，英文按词数。宁可高估，别把窗口塞爆。
    static func approxTokens(_ s: String) -> Int {
        let cjk = s.filter { $0.isCJK }.count
        let words = s.split(whereSeparator: { $0.isWhitespace }).count
        return cjk + max(0, words - cjk / 2) + 4
    }

    static func pack(_ text: String, maxTokens: Int) -> [String] {
        var chunks: [String] = []
        var cur = ""
        for s in sentences(text) {
            if approxTokens(s) > maxTokens {                      // 单句过长 → 按 token 硬切
                if !cur.isEmpty { chunks.append(cur); cur = "" }
                var piece = ""
                for ch in s {
                    piece.append(ch)
                    if approxTokens(piece) >= maxTokens { chunks.append(piece); piece = "" }
                }
                cur = piece
                continue
            }
            if approxTokens(cur + s) > maxTokens, !cur.isEmpty { chunks.append(cur); cur = s }
            else { cur += s }
        }
        if !cur.isEmpty { chunks.append(cur) }
        return chunks.isEmpty ? [text] : chunks
    }
}

enum OutputSanitizer {
    /// 结束/角色 token 的文本形态（用片段拼出来，避免源码里出现完整特殊 token 串——
    /// 那些串在很多编辑器和 diff 工具里会被当成控制序列，也会在日志里造成混乱）。
    static let roleJunk: [String] = ["<|" + "hy_" + "Assistant" + "|>", "<|" + "hy_" + "User" + "|>",
                                     "<|" + "hy_" + "place" + "\u{2581}" + "holder" + "\u{2581}" + "no" + "\u{2581}" + "2" + "|>",
                                     "<|" + "hy_" + "begin" + "\u{2581}" + "of" + "\u{2581}" + "sentence" + "|>"]

    static func clean(_ raw: String) -> String {
        var s = raw
        for junk in roleJunk { s = s.replacingOccurrences(of: junk, with: "") }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// 输出与输入高度重合 = 模型没翻，只是抄。
    static func looksLikeSource(_ out: String, _ src: String) -> Bool {
        guard !out.isEmpty else { return true }
        if out == src.trimmingCharacters(in: .whitespacesAndNewlines) { return true }
        let a = Set(out.unicodeScalars.prefix(200))
        let b = Set(src.unicodeScalars.prefix(200))
        guard !a.isEmpty else { return true }
        return Double(b.intersection(a).count) / Double(a.count) > 0.95 && out.count > 8
    }

    /// 最低可用校验（对应 core/validate.py 的三条红线）：非空、方向正确、长度不离谱。
    static func acceptable(_ out: String, source: String, targetIsChinese: Bool) -> Bool {
        if out.count < 2 { return false }
        let srcCJK = Double(source.filter { $0.isCJK }.count) / Double(max(1, source.count))
        let outCJK = Double(out.filter { $0.isCJK }.count) / Double(max(1, out.count))
        // 目标中文时译文必须以中文为主；目标英文时必须基本是拉丁字母
        let okScript = targetIsChinese ? outCJK > 0.6 : outCJK < 0.2
        let notEchoOfSource = targetIsChinese ? srcCJK < 0.6 || outCJK > 0.6 : srcCJK > 0.6 || outCJK < 0.2
        let ratio = Double(out.count) / Double(max(1, source.count))
        return okScript && notEchoOfSource && ratio > 0.25 && ratio < 4.5
    }
}

extension Character {
    // 全仓唯一的 isCJK（LocalMTApp.swift 里的同名扩展已删除：同模块重复声明会编译失败）。
    // 区间是两处旧定义的并集：CJK 标点/假名/扩展A/统一表意/兼容表意/全角形式。
    var isCJK: Bool {
        guard let v = unicodeScalars.first?.value else { return false }
        return (0x3000...0x303F).contains(v)
            || (0x3040...0x30FF).contains(v)
            || (0x3400...0x4DBF).contains(v)
            || (0x4E00...0x9FFF).contains(v)
            || (0xF900...0xFAFF).contains(v)
            || (0xFF00...0xFFEF).contains(v)
    }
}
