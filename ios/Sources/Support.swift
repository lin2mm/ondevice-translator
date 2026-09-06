import Foundation

/// 语言方向：本方案只做中英，所以枚举而不是通用映射表（少一层出错可能）。
/// `targetFullName` 用全称是实测结论——模型对 "Chinese"/"English" 这种短名会漂移成解释句子，
/// 全称（Simplified Chinese / American English）才稳定（docs/03 第 4 节）。
enum LangPair {
    struct Direction: Hashable {
        let isToChinese: Bool
        var targetFullName: String { isToChinese ? "Simplified Chinese" : "American English" }
        var sourceFullName: String { isToChinese ? "American English" : "Simplified Chinese" }
    }

    static func direction(source: Locale.Language, target: Locale.Language) -> Direction {
        // 判定以"目标语言"为准，来源只做兜底：用户可能从任一方向点翻译
        let t = target.identifier
        if t.hasPrefix("zh") { return Direction(isToChinese: true) }
        if t.hasPrefix("en") { return Direction(isToChinese: false) }
        let s = source.identifier
        return Direction(isToChinese: !s.hasPrefix("zh"))
    }
}

/// 术语表（对应 core/glossary.py）。四种模式里 App 只用两种：
///   hint   —— 翻译前把提示喂给模型（软约束，允许模型在语境不合时偏离）
///   enforce—— 翻译后做替换兜底（硬约束，用于品牌词/法规词）
/// keep / forbid 是给"保留原文"和"禁用词"用的，UI 里已提供，语义与 core 一致。
struct GlossaryEntry: Hashable {
    enum Mode: String { case hint, enforce, keep, forbid }
    let src: String
    let dst: String
    let mode: Mode
    let caseSensitive: Bool
}

final class Glossary: @unchecked Sendable {
    static let shared = Glossary()

    private(set) var entries: [GlossaryEntry] = []
    private let lock = NSLock()
    /// 术语表一改就 +1，引擎缓存 key 里带着它，避免"改了术语表还用旧译文"。
    private(set) var version = 0

    init() { reload() }

    /// 文件位置：iCloud 容器优先（跨设备同步），没有就用 App 沙盒；bundle 里带一份出厂表。
    static var fileURL: URL {
        let fm = FileManager.default
        if let icloud = fm.url(forUbiquityContainerIdentifier: nil)?
            .appendingPathComponent("Documents", isDirectory: true) {
            try? fm.createDirectory(at: icloud, withIntermediateDirectories: true)
            return icloud.appendingPathComponent("glossary.tsv")
        }
        let docs = fm.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appendingPathComponent("glossary.tsv")
    }

    func reload() {
        var loaded: [GlossaryEntry] = []
        let url = Glossary.fileURL
        let fm = FileManager.default
        let readFrom: URL? = fm.fileExists(atPath: url.path) ? url
            : Bundle.main.url(forResource: "glossary", withExtension: "tsv")
        if let readFrom, let text = try? String(contentsOf: readFrom, encoding: .utf8) {
            loaded = Glossary.parse(text)
        }
        lock.lock(); entries = loaded; version += 1; lock.unlock()
    }

    static func parse(_ text: String) -> [GlossaryEntry] {
        var out: [GlossaryEntry] = []
        for line in text.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let s = line.trimmingCharacters(in: .whitespaces)
            if s.isEmpty || s.hasPrefix("#") { continue }
            let cols = s.components(separatedBy: "\t")
            guard cols.count >= 2 else { continue }
            let mode = GlossaryEntry.Mode(rawValue: cols.count > 2 ? cols[2] : "hint") ?? .hint
            out.append(.init(src: cols[0], dst: cols[1], mode: mode,
                             caseSensitive: cols.count > 3 && cols[3] == "sensitive"))
        }
        return out
    }

    /// 给 prompt 的软提示（只要命中当前方向的 hint/keep 条目）。
    func hints(for text: String, direction: LangPair.Direction) -> [String] {
        lock.lock(); let all = entries; lock.unlock()
        return all.compactMap { e in
            guard e.mode == .hint || e.mode == .keep else { return nil }
            guard contains(text, e.src, sensitive: e.caseSensitive) else { return nil }
            return e.mode == .keep ? "Keep verbatim: \(e.src)" : "Use \(e.dst) for \(e.src)"
        }
    }

    /// 翻译后的硬替换 + 禁用词检查（禁用词命中不改文本，交给上层提示用户）。
    func apply(to out: String, direction: LangPair.Direction) -> String {
        lock.lock(); let all = entries; lock.unlock()
        var s = out
        for e in all where e.mode == .enforce && contains(s, e.src, sensitive: e.caseSensitive) {
            s = replace(s, e.src, with: e.dst, sensitive: e.caseSensitive)
        }
        return s
    }

    func forbiddenHits(in out: String) -> [String] {
        lock.lock(); let all = entries; lock.unlock()
        return all.filter { $0.mode == .forbid && contains(out, $0.dst, sensitive: $0.caseSensitive) }
            .map { $0.dst }
    }

    private func contains(_ hay: String, _ needle: String, sensitive: Bool) -> Bool {
        if needle.isEmpty { return false }
        return sensitive ? hay.range(of: needle) != nil
                         : hay.lowercased().contains(needle.lowercased())
    }

    private func replace(_ hay: String, _ needle: String, with sub: String, sensitive: Bool) -> String {
        if sensitive { return hay.replacingOccurrences(of: needle, with: sub) }
        return hay.replacingOccurrences(of: needle, with: sub, options: .caseInsensitive)
    }
}

/// 自学习 L1：用户改过的句子进 TM，下次同义句做 few-shot。
/// 表结构与打分规则见 core/learn.py（SQLite）；iOS 先用 JSON 文件跑通，阶段 3 换 SQLite/GRDB。
final class TranslationMemory: @unchecked Sendable {
    struct Item { let src: String; let dst: String; let weight: Double }

    private var items: [Item] = []
    private let lock = NSLock()

    init() { load() }

    private var url: URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        return docs.appendingPathComponent("tm.json")
    }

    private func load() {
        guard let data = try? Data(contentsOf: url),
              let raw = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }
        let parsed = raw.compactMap { d -> Item? in
            guard let s = d["src"] as? String, let t = d["dst"] as? String else { return nil }
            return Item(src: s, dst: t, weight: (d["w"] as? Double) ?? 0.4)
        }
        lock.lock(); items = parsed; lock.unlock()
    }

    /// 记录一次编辑：用户改过 = 1.0，只是复制没改 = 0.4（与 core/learn.py 的 record_edit 一致）。
    func record(source: String, finalText: String, edited: Bool) {
        let w = edited ? 1.0 : 0.4
        lock.lock()
        items.removeAll { $0.src == source }
        items.append(Item(src: source, dst: finalText, weight: w))
        if items.count > 20_000 { items.removeFirst(items.count - 20_000) }   // 上限同 core
        let snapshot = items
        lock.unlock()
        if let data = try? JSONSerialization.data(withJSONObject: snapshot.map {
            ["src": $0.src, "dst": $0.dst, "w": $0.weight] }), (try? data.write(to: url)) != nil { return }
    }

    /// 相似度用字符 3-gram Jaccard + 长度惩罚（core/learn.py 的算法），阈值 0.34。
    func fewShot(for text: String, direction: LangPair.Direction, k: Int = 3, minSim: Double = 0.34) -> [String] {
        lock.lock(); let all = items; lock.unlock()
        let base = Self.tri(text)
        let scored = all.compactMap { it -> (Double, Item)? in
            let sim = Self.jaccard(base, Self.tri(it.src))
            guard sim >= minSim else { return nil }
            let lp = 1.0 - abs(Double(it.src.count - text.count)) / Double(max(text.count, it.src.count, 1)) * 0.3
            return (sim * lp * it.weight, it)
        }
        return scored.sorted { $0.0 > $1.0 }.prefix(k).map { $0.1.dst }
    }

    private static func tri(_ s: String) -> Set<String> {
        let c = Array(s)
        guard c.count >= 3 else { return c.isEmpty ? [] : [String(c)] }
        var out = Set<String>()
        for i in 0...(c.count - 3) { out.insert(String(c[i..<(i + 3)])) }
        return out
    }

    private static func jaccard(_ a: Set<String>, _ b: Set<String>) -> Double {
        guard !a.isEmpty || !b.isEmpty else { return 0 }
        let inter = Double(a.intersection(b).count)
        let uni = Double(a.union(b).count)
        return uni > 0 ? inter / uni : 0
    }
}
