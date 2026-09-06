import Foundation
import os

/// 唯一要实现的接口（等价于 core/engine.py 的 Engine 协议）。
/// 两个实现：HyMTLlamaEngine（A 档，包内权重 + llama.cpp）、AppleTranslationEngine（C 档，系统框架）。
public protocol TranslationEngine: AnyObject, Sendable {
    var id: String { get }
    var isReady: Bool { get }
    func prepare() async throws
    func generate(prompt: Prompt, maxNewTokens: Int, sampling: SamplingParams,
                  onToken: ((String) -> Void)?, shouldCancel: @escaping () -> Bool) async throws -> String
    func unload()
}

public struct Prompt: Sendable {
    public let user: String
    public let templateID: String
    public init(user: String, templateID: String = "default") { self.user = user; self.templateID = templateID }
}

public struct SamplingParams: Sendable {
    public var temperature: Float = 0.7
    public var topP: Float = 0.6
    public var topK: Int32 = 20
    public var repeatPenalty: Float = 1.05
    public init() {}
    /// 两档与 core/prompt.py 的 HYMT2_SAMPLING / HYMT2_SAMPLING_SAFE 逐字一致。
    /// 注意：bridge 按 temperature < 0.5 选择 LTSampling.retry() —— 所以 safe 的温度
    /// 必须是 0.3（原占位写法温度 0.7 会导致降档重试永远不触发）。
    public static let standard = SamplingParams()
    public static let safe: SamplingParams = {
        var s = SamplingParams()
        s.temperature = 0.3; s.topP = 0.8; s.topK = 40; s.repeatPenalty = 1.08
        return s
    }()
}

public enum EngineError: LocalizedError {
    case modelMissing, loadFailed(String), outOfMemory, cancelled

    public var errorDescription: String? {
        switch self {
        case .modelMissing: return "App 包内没有权重：用 ./ios/install-on-iphone15.sh --model small 重新打包"
        case .loadFailed(let m): return "本地模型加载失败：\(m)"
        case .outOfMemory: return "内存不足，已回落到系统离线翻译"
        case .cancelled: return "已取消"
        }
    }
}

/// A 档：llama.cpp + 内置 Hy-MT2 GGUF。真正的 C API 调用都在 ObjC++ 的 LTLlamaBridge 里
/// （Swift/C++ interop 要对整条依赖树设置，容易炸；ObjC++ + bridging header 零配置）。
public final class HyMTLlamaEngine: TranslationEngine, @unchecked Sendable {
    public let id = "hy-mt2-llama-cpp"
    public private(set) var isReady = false

    private let bridge = LTLlamaBridge()
    private let modelURL: URL
    private let nCtx: Int32
    private let threads: Int32
    private let gpuLayers: Int32
    private let q = DispatchQueue(label: "dev.localmt.infer", qos: .userInitiated)
    private let log = Logger(subsystem: "dev.localmt", category: "engine")
    private var pressure: DispatchSourceMemoryPressure?

    /// 从 App bundle 里找内置权重（install 脚本会把它拷进 Resources/models）。
    /// 找不到就返回 nil —— 上层据此自动回落 C 档，而不是崩。
    public static func bundledModel() -> URL? {
        for name in ["Hy-MT2-1.8B-1.25Bit", "Hy-MT2-1.8B-Q4_K_M"] {
            if let u = Bundle.main.url(forResource: name, withExtension: "gguf", subdirectory: "models")
                ?? Bundle.main.url(forResource: name, withExtension: "gguf") { return u }
        }
        return nil
    }

    public init?(modelURL: URL? = HyMTLlamaEngine.bundledModel(), ctx: Int32 = 2048,
                 threads: Int32 = 4, gpuLayers: Int32 = 999) {
        guard let modelURL else { return nil }
        self.modelURL = modelURL
        self.threads = threads
        // 只有 6GB 以上的机器才给 Metal 全 offload；小机器 CPU 更稳（GPU 会挤内存）
        let big = ProcessInfo.processInfo.physicalMemory > 5 * 1000 * 1000 * 1000
        self.gpuLayers = big ? gpuLayers : 0
        self.nCtx = big ? ctx : 1024
    }

    public func prepare() async throws {
        if isReady { return }
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            q.async {
                guard self.bridge.loadModelAtPath(self.modelURL.path, nCtx: Int32(self.nCtx),
                                                  threads: self.threads, gpuLayers: self.gpuLayers) else {
                    c.resume(throwing: EngineError.loadFailed("加载失败：确认 llama.cpp 支持 hunyuan-dense 与该量化类型")); return
                }
                self.isReady = true
                // 这行日志是"慢得莫名其妙"的唯一现场证据：必须出现 METAL
                self.log.info("loaded backend=\(self.bridge.backendInfo()) ctx=\(self.nCtx) mmap=on")
                self.watchMemory()
                c.resume()
            }
        }
    }

    /// 内存告警就交还模型：6GB 机器上"常驻"是负资产（后台被杀 + 耗电差评）。
    private func watchMemory() {
        let src = DispatchSource.makeMemoryPressureSource(eventMask: [.warning, .critical], queue: q)
        src.setEventHandler { [weak self] in
            guard let self else { return }
            let ev = src.data ?? []
            if ev.contains(.critical) { self.unload(); self.log.error("memory critical -> unloaded") }
            else { self.log.notice("memory warning -> 建议降档") }
        }
        src.resume()
        pressure = src
    }

    public func unload() {
        q.sync {
            pressure?.cancel(); pressure = nil
            bridge.unload(); isReady = false
        }
    }

    public func generate(prompt: Prompt, maxNewTokens: Int, sampling: SamplingParams,
                         onToken: ((String) -> Void)?,
                         shouldCancel: @escaping () -> Bool) async throws -> String {
        if !isReady { try await prepare() }
        return try await withCheckedThrowingContinuation { (c: CheckedContinuation<String, Error>) in
            q.async {
                if shouldCancel() { self.bridge.requestCancel() }
                let sp = sampling.temperature < 0.5 ? LTSampling.retry() : LTSampling.defaults()
                do {
                    let out = try self.bridge.translateUserText(prompt.user, maxNewTokens: Int32(maxNewTokens),
                                                                sampling: sp,
                                                                onToken: { partial in onToken?(partial) })
                    c.resume(returning: out ?? "")
                } catch {
                    c.resume(throwing: EngineError.loadFailed(error.localizedDescription))
                }
            }
        }
    }
}

/// C 档：系统 Translation 框架（见 EngineFactory.swift）。扩展（键盘/分享）里只能用这一档。
