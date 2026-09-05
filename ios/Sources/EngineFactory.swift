import Foundation
import Translation
import AVFoundation

/// 引擎选择：阶段 1 只有 C 档（系统离线翻译，零下载）；阶段 2 在此插入 A 档（Hy-MT2 + llama.cpp）。
/// 契约与 core/engine.py 一致：给文本 + 方向，返回译文；失败必须抛错而不是返回原文
///（返回原文是最恶劣的失败模式：`validate.check` 会把它判成 passthrough，而 UI 看起来"正常"）。
enum EngineFactory {
    static let shared: TranslateEngine = make()

    private static func make() -> TranslateEngine {
        // 包内有权重 → 本地 LLM（真离线、可注入术语表/自学习）；否则回落系统框架。
        // 注意返回的是 LocalLLMTranslateEngine（分句/术语/校验的完整链路），
        // 不是裸的 HyMTLlamaEngine —— 后者只实现 generate()，不满足 TranslateEngine
        // 协议的 translate()（直接返回会编译失败，这是本分支修的第三个编译错误）。
        if HyMTLlamaEngine.bundledModel() != nil, let llm = HyMTLlamaEngine() {
            return LocalLLMTranslateEngine(llm: llm)
        }
        return AppleTranslationEngine()
    }

    static var activeDescription: String {
        shared is LocalLLMTranslateEngine
            ? "Hy-MT2-1.8B · 本地推理（\(BundledModel.summary)）"
            : "系统离线翻译(Translation.framework) · 未内置权重"
    }

enum BundledModel {
    static let names = ["Hy-MT2-1.8B-1.25Bit", "Hy-MT2-1.8B-Q4_K_M"]
    static var urls: [URL] {
        names.compactMap { Bundle.main.url(forResource: $0, withExtension: "gguf", subdirectory: "models")
                             ?? Bundle.main.url(forResource: $0, withExtension: "gguf") }
    }
    static var present: Bool { !urls.isEmpty }
    static var summary: String {
        urls.reduce(into: "") { acc, u in
            let mb = (try? u.resourceValues(forKeys: [.fileSizeKey]).fileSize).map { $0 / 1_000_000 } ?? 0
            acc += acc.isEmpty ? "\(mb)MB" : " + \(mb)MB"
        }
    }
}
}

protocol TranslateEngine {
    func translate(_ text: String, source: Locale.Language, target: Locale.Language) async throws -> String
}

enum EngineFailure: LocalizedError {
    case packMissing(Locale.Language)
    case tooLong(Int)
    case unsupportedOS
    case inner(String)

    var errorDescription: String? {
        switch self {
        case .packMissing(let l): return "缺少 \(l.maximalIdentifier) 离线语言包：设置 → App 与 翻译数据（或直接点“下载语言包”）"
        case .tooLong(let n): return "单次过长（\(n) 字）：请分段，或等 A 档本地模型接入后自动分句"
        case .unsupportedOS: return "系统直接翻译 API 需 iOS 26+；iOS 18/19 请用 .translationTask 修饰符或走自带模型"
        case .inner(let m): return m
        }
    }
}

/// C 档：Apple `Translation` 框架。模型由系统托管（不占我们的包体积，也不归我们管）。
/// 能力边界要说清：**无法注入术语表/few-shot**，所以它只做兜底，不是主力。
struct AppleTranslationEngine: TranslateEngine {
    func translate(_ text: String, source: Locale.Language, target: Locale.Language) async throws -> String {
        let availability = LanguageAvailability()
        let status = await availability.status(from: source, to: target)
        switch status {
        case .installed:
            break
        default:
            // 统一抛可见错误（产品规则：C 档失败必须显式报错，绝不无声兜底）。
            // 原代码 switch 了 .downloadRequired —— 该 case 在 iOS 26 SDK 的
            // LanguageAvailability.Status 里已不存在（现存 installed/supported/unsupported），
            // 编译会失败；且 TranslationSession.prepareTranslation(for:) 静态方法查无此 API
            //（prepareTranslation() 是实例方法，session 需经 .translationTask 或 iOS 26 直接
            // 构造获得）。语言包引导下载留到阶段 2 用真 UI 做，这里先把错误报清楚。
            throw EngineFailure.packMissing(target)
        }

        guard #available(iOS 26.0, *) else { throw EngineFailure.unsupportedOS }
        // iOS 26 起可在 SwiftUI 修饰符之外直接构造 session（此前必须在 .translationTask 里拿）
        let session = TranslationSession(installedSource: source, target: target)
        do {
            let response = try await session.translate(text)
            let out = response.targetText
            guard !out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw EngineFailure.inner("系统返回空译文")
            }
            return out
        } catch let e {
            throw EngineFailure.inner("翻译失败：\(e.localizedDescription)")
        }
    }
}

/// 系统 TTS（不联网）。用 AVSpeechSynthesizer —— Speech 框架是**识别**不是合成，别搞混。
public final class SpeechReader: NSObject, AVSpeechSynthesizerDelegate {
    static let shared = SpeechReader()
    private let synth = AVSpeechSynthesizer()

    func speak(_ text: String, useChineseVoice: Bool) {
        synth.stopSpeaking(at: .immediate)
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: useChineseVoice ? "zh-CN" : "en-US")
        u.rate = useChineseVoice ? 0.5 : 0.45
        u.volume = 1.0
        synth.delegate = self
        synth.speak(u)
    }
}
