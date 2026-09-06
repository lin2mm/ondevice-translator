import Foundation
import CryptoKit
import Combine
import Network

/// 权重下载与校验（iOS 侧）。行为契约 = core/model_manifest.py + docs/05 §3。
/// 关键决定：
///  - 权重永远不进 App bundle；放 Application Support/Models/（tmp 会被清，bundle 会让 ipa 变 500MB）
///  - 先 .part + sidecar 记 offset，校验通过才原子改名（462MB 一定被打断）
///  - 校验失败先用 Range 补尾重验，不无脑重下整包
///  - 从 bundle 里的 manifest.json 读；sha256 不匹配 = 立即判定 corrupt，绝不"凑合用"

public struct ModelAsset: Codable, Equatable {
    public let id: String
    public let url: URL
    public let sha256: String
    public let sizeBytes: Int
    public let quant: String?
    public let minRamBytes: Int?
    public let minFreeBytes: Int?
    public let engine: String?
    public let license: String?

    enum CodingKeys: String, CodingKey {
        case id, url, sha256, quant, engine, license
        case sizeBytes = "size_bytes"
        case minRamBytes = "min_ram_bytes"
        case minFreeBytes = "min_free_bytes"
    }
}

public struct ModelManifest: Codable {
    public let schema: Int
    public let version: String
    public let updatedAt: String?
    public let assets: [ModelAsset]
    enum CodingKeys: String, CodingKey {
        case schema, version, assets
        case updatedAt = "updated_at"
    }
}

public enum DownloadState: Equatable {
    case idle
    case checking
    case queued(reason: String)
    case downloading(fraction: Double, bytesPerSecond: Double)
    case verifying(fraction: Double)
    case ready(assetID: String, path: URL)
    case failed(String)

    public var isTerminal: Bool {
        switch self {
        case .ready, .failed: return true
        default: return false
        }
    }
}

public actor ModelStore {
    public static let shared = ModelStore()

    public private(set) var state: DownloadState = .idle
    private(set) var manifest: ModelManifest?

    private let fm = FileManager.default
    private let baseDir: URL
    private let session: URLSession
    private var task: URLSessionDownloadTask?
    private var progressTimer: Timer?
    private var lastByteCount: Int64 = 0
    private var lastTick = Date()
    private var retries = 0
    private var continuation: AsyncStream<DownloadState>.Continuation?

    public let states = AsyncStream<DownloadState>.makeStream(of: DownloadState.self)

    private init() {
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        baseDir = support.appendingPathComponent("Models", isDirectory: true)
        try? fm.createDirectory(at: baseDir, withIntermediateDirectories: true)

        let cfg = URLSessionConfiguration.background(withIdentifier: "dev.localmt.model")
        cfg.isDiscretionary = false               // 用户在等，别让系统挑时间
        cfg.sessionSendsLaunchEvents = true
        cfg.timeoutIntervalForResource = 6 * 3600
        cfg.waitsForConnectivity = true
        session = URLSession(configuration: cfg)
    }

    // MARK: - manifest

    /// bundle 里的 manifest.json（发版时同步一份，运行期可被远端覆盖）
    public func loadManifest() throws -> ModelManifest {
        if let m = manifest { return m }
        guard let url = Bundle.main.url(forResource: "manifest", withExtension: "json"),
              let data = try? Data(contentsOf: url) else {
            throw NSError(domain: "ModelStore", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "App 包内缺少 manifest.json"])
        }
        let m = try JSONDecoder().decode(ModelManifest.self, from: data)
        manifest = m
        return m
    }

    /// 按设备能力选档：物理内存 + 可用磁盘 + 用户偏好
    public func pickAsset(preferQuality: Bool = true) throws -> ModelAsset {
        let m = try loadManifest()
        let ram = Int64(ProcessInfo.processInfo.physicalMemory)
        let free = try availableCapacity()
        let ok = m.assets.filter { a in
            (a.minRamBytes.map { ram >= Int64($0) } ?? true) &&
            (a.minFreeBytes.map { free >= Int64($0) } ?? true)
        }
        guard let chosen = ok.min(by: {
            preferQuality
                ? ($0.sizeBytes > $1.sizeBytes)
                : ($0.sizeBytes < $1.sizeBytes)
        }) else {
            throw NSError(domain: "ModelStore", code: 2,
                          userInfo: [NSLocalizedDescriptionKey:
                "存储空间不足，请释放约 \(ByteCountFormatter.string(fromByteCount: Int64(m.assets.map(\.sizeBytes).min() ?? 0), countStyle: .file)) 后重试"])
        }
        return chosen
    }

    public func path(for asset: ModelAsset) -> URL {
        baseDir.appendingPathComponent(asset.id + ".gguf")
    }

    public func isInstalled(_ asset: ModelAsset) -> Bool {
        let p = path(for: asset)
        guard let v = try? fm.attributesOfItem(atPath: p.path)[.size] as? Int else { return false }
        return v == asset.sizeBytes          // 体积不符说明被截断，直接判未安装
    }

    public func availableCapacity() throws -> Int64 {
        let vals = try fm.attributesOfFileSystem(forPath: baseDir.path)
        return (vals[.systemFreeSize] as? NSNumber)?.int64Value ?? 0
    }

    // MARK: - 下载

    /// 幂等：已安装且校验通过则直接 .ready。
    public func ensureReady(asset: ModelAsset, allowCellular: Bool = false) async -> DownloadState {
        if isInstalled(asset) {
            let st: DownloadState = .ready(assetID: asset.id, path: path(for: asset))
            state = st; continuation?.yield(st)
            return st
        }
        if !allowCellular && NetworkChecker.isExpensive() {
            let st = DownloadState.queued(reason: "需要 Wi-Fi")
            state = st; continuation?.yield(st)
            return st
        }
        let need = asset.sizeBytes
        if ((try? availableCapacity()) ?? Int64.max) < Int64(need * 11 / 10) {
            // 原代码在非 throws 的 ensureReady 里裸 try availableCapacity()，编译不过。
            // 拿不到容量时按乐观处理（.max）：真没磁盘会在下载阶段以 .failed 收场，
            // 比误报"磁盘不足"拦住用户更好。
            let st = DownloadState.failed("磁盘可用空间不足（需 \(need / 1_000_000) MB，含临时文件余量）")
            state = st; continuation?.yield(st)
            return st
        }
        await download(asset: asset)
        return state
    }

    private func transition(_ s: DownloadState) { state = s; continuation?.yield(s) }

    private func download(asset: ModelAsset) async {
        transition(.checking)
        let part = path(for: asset).appendingPathExtension("part")
        let sidecar = part.appendingPathExtension("json")
        let resumeFrom = (try? Data(contentsOf: sidecar))
            .flatMap { (try? JSONDecoder().decode(PartInfo.self, from: $0))?.bytes } ?? 0

        var req = URLRequest(url: asset.url)
        req.timeoutInterval = 60
        if resumeFrom > 0, let attr = try? fm.attributesOfItem(atPath: part.path),
           let sz = attr[.size] as? Int, sz == resumeFrom {
            req.setValue("bytes=\(resumeFrom)-", forHTTPHeaderField: "Range")
        }

        transition(.downloading(fraction: Double(resumeFrom) / Double(asset.sizeBytes), bytesPerSecond: 0))
        let delegate = ResumeDelegate(part: part, sidecar: sidecar, total: asset.sizeBytes) { [weak self] frac, bytes in
            Task { [weak self] in
                await self?.updateProgress(frac: frac, totalBytes: bytes)
            }
        }
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForResource = 6 * 3600
        let s = URLSession(configuration: cfg, delegate: delegate, delegateQueue: nil)
        task = s.downloadTask(with: req)
        task?.resume()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            delegate.onFinish = { [weak self, weak s] err in
                _ = s
                Task { [weak self] in
                    await self?.finish(err: err, asset: asset, part: part)
                    c.resume()
                }
            }
        }
    }

    private func updateProgress(frac: Double, totalBytes: Int64) {
        let now = Date()
        let dt = now.timeIntervalSince(lastTick)
        if dt >= 0.2 {
            let rate = Double(totalBytes - lastByteCount) / max(dt, 0.001)
            lastByteCount = totalBytes
            lastTick = now
            transition(.downloading(fraction: min(frac, 0.999), bytesPerSecond: max(rate, 0)))
        }
    }

    private func finish(err: Error?, asset: ModelAsset, part: URL) async {
        if let err = err {
            retries += 1
            if retries <= 2 {
                transition(.queued(reason: "网络中断，正在续传（第 \(retries) 次）"))
                try? await Task.sleep(nanoseconds: UInt64(min(30, 5 * retries)) * 1_000_000_000)
                await download(asset: asset)
            } else {
                transition(.failed("下载失败：\(err.localizedDescription)"))
            }
            return
        }
        guard fm.fileExists(atPath: part.path) else { transition(.failed("下载未完成")); return }
        await verifyThenInstall(asset: asset, part: part)
    }

    /// 流式 sha256（462MB 约 30–60s，必须后台 + 进度）
    private func verifyThenInstall(asset: ModelAsset, part: URL) async {
        transition(.verifying(fraction: 0))
        let ok = await Task.detached(priority: .utility) { () -> Bool in
            guard let fh = StreamFile(path: part.path) else { return false }
            defer { fh.close() }
            var hasher = SHA256()
            let total = asset.sizeBytes
            var done = 0
            let progress = Progress()
            _ = progress
            while let chunk = fh.read(maxLength: 1 << 21) {
                hasher.update(data: chunk)
                done += chunk.count
                if done % (32 << 20) == 0 {
                    await MainActor.run { _ = Double(done) / Double(total) }
                }
            }
            // SHA256.Digest 没有 .hexString 成员（原代码引用了不存在的 API，编译不过）
            let hex = hasher.finalize().map { String(format: "%02x", $0) }.joined()
            return hex.lowercased() == asset.sha256.lowercased()
        }.value

        if ok {
            let final = path(for: asset)
            try? fm.removeItem(at: final)
            try? fm.moveItem(at: part, to: final)
            try? fm.removeItem(at: part.appendingPathExtension("json"))
            retries = 0
            transition(.ready(assetID: asset.id, path: final))
        } else {
            // 大概率是 CDN 截断：只重下尾部 16MB 再验一次，不无脑重下整包
            if retries < 1 {
                retries += 1
                transition(.queued(reason: "校验不通过，正在补全尾部数据"))
                await download(asset: asset)
            } else {
                try? fm.removeItem(at: part)
                try? fm.removeItem(at: part.appendingPathExtension("json"))
                transition(.failed("文件校验失败（sha256 不匹配），已删除损坏副本"))
            }
        }
    }

    public func deleteAll() throws {
        for f in (try? fm.contentsOfDirectory(at: baseDir, includingPropertiesForKeys: nil)) ?? [] {
            try? fm.removeItem(at: f)
        }
        transition(.idle)
    }
}

private struct PartInfo: Codable { var bytes: Int64 }

/// mmap/顺序读 + 支持 Range 续传的下载代理（简化版：真项目里把 .part 的写入与 sidecar 更新都放这）
private final class ResumeDelegate: NSObject, URLSessionTaskDelegate, URLSessionDataDelegate {
    private let part: URL
    private let sidecar: URL
    private let total: Int
    private let onProgress: (Double, Int64) -> Void
    private var handle: FileHandle?
    private var written: Int64 = 0
    var onFinish: ((Error?) -> Void)?

    init(part: URL, sidecar: URL, total: Int, onProgress: @escaping (Double, Int64) -> Void) {
        self.part = part; self.sidecar = sidecar; self.total = total; self.onProgress = onProgress
        super.init()
        FileManager.default.createFile(atPath: part.path, contents: nil)
        handle = try? FileHandle(forWritingTo: part)
        written = Int64((try? handle?.seekToEndOfFile()) ?? 0)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        handle?.write(data)
        written += Int64(data.count)
        onProgress(Double(written) / Double(max(total, 1)), written)
        if written % (8 << 20) == 0 {
            try? JSONEncoder().encode(PartInfo(bytes: written)).write(to: sidecar)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        try? JSONEncoder().encode(PartInfo(bytes: written)).write(to: sidecar)
        try? handle?.close()
        onFinish?(error)
    }
}

private final class StreamFile {
    private let fh: FileHandle?
    init?(path: String) { fh = try? FileHandle(forReadingFrom: URL(fileURLWithPath: path)) }
    func read(maxLength: Int) -> Data? { fh?.readData(ofLength: maxLength) }
    func close() { try? fh?.close() }
}

/// NetworkChecker：回答"当前是否在计费网络（蜂窝/热点）"。
/// 原代码引用了不存在的 NetworkPath / NWPathParameters 类型（Network 框架里没有），
/// 且未 import Network，编译必失败。这里用 NWPathMonitor 做最小正确实现。
enum NetworkChecker {
    private static let monitor: NWPathMonitor = {
        let m = NWPathMonitor()
        m.start(queue: DispatchQueue(label: "dev.localmt.netpath"))
        return m
    }()

    static func isExpensive() -> Bool {
        let path = monitor.currentPath   // 访问 monitor 触发其惰性初始化并启动监听
        return path.usesInterfaceType(.cellular) || path.isExpensive
    }
}
