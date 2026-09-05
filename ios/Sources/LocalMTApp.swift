import SwiftUI
import Foundation

/// 阶段 1（今天就能在 iPhone 15 上装并跑起来）：只依赖系统 `Translation` 框架 ——
/// 零第三方依赖、零权重下载、纯离线，用来验证 UI/交互/延迟曲线，也验证你的签名链路。
/// 阶段 2：把 HyMTLlamaEngine 接进来（见 Sources/TranslationEngine.swift + docs/03 §3），
///          在 EngineFactory 里换成 A 档，其余 UI 代码一行不改。

@main
struct LocalMTApp: App {
    @StateObject private var vm = TranslateViewModel()

    var body: some Scene {
        WindowGroup {
            ContentView(vm: vm)
                .environmentObject(vm)
                .task { await vm.boot() }
        }
    }
}

@MainActor
final class TranslateViewModel: ObservableObject {
    @Published var input = ""
    @Published var output = ""
    @Published var engineName = "—"
    @Published var isTranslating = false
    @Published var elapsed: Double?
    @Published var error: String?
    /// auto / zh2en / en2zh
    @Published var direction: Direction = .auto
    @Published var glossaryText = ""          // 阶段 2 接 enforce/hint 术语表
    @Published var history: [Entry] = []

    struct Entry: Identifiable, Hashable {
        let id = UUID()
        let src: String
        let dst: String
        let ms: Double
        let engine: String
        let at = Date()
    }

    enum Direction: String, CaseIterable, Identifiable {
        case auto = "自动", zh2en = "中→英", en2zh = "英→中"
        var id: String { rawValue }
    }

    var speakingIsChinese: Bool {
        direction == .en2zh || (direction == .auto && output.contains { $0.isCJK })
    }

    private let started = Date()
    func boot() async {
        engineName = await EngineFactory.activeDescription
    }

    func translate() async {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { output = ""; error = nil; return }
        isTranslating = true
        error = nil
        let t0 = Date()
        defer {
            elapsed = Date().timeIntervalSince(t0)
            isTranslating = false
        }
        do {
            let (src, tgt) = resolveDirection(text)
            let out = try await EngineFactory.shared.translate(text, source: src, target: tgt)
            output = out
            history.insert(.init(src: text, dst: out, ms: (Date().timeIntervalSince(t0)) * 1000,
                                 engine: engineName), at: 0)
            history = Array(history.prefix(50))
        } catch {
            self.error = error.localizedDescription
            output = ""
        }
    }

    private static let zh = Locale.Language(identifier: "zh-Hans")
    private static let en = Locale.Language(identifier: "en")

    private func resolveDirection(_ text: String) -> (Locale.Language, Locale.Language) {
        // 二分类够用（中英产品）：CJK 占比 > 25% 判为中文。与 core/engine.py detect() 同口径。
        let cjk = text.filter { $0.isCJK }.count
        let latin = text.filter { $0.isASCII && $0.isLetter }.count
        let isZh = cjk > 0 && Double(cjk) / Double(max(1, cjk + latin)) >= 0.25
        switch direction {
        case .zh2en: return (Self.zh, Self.en)
        case .en2zh: return (Self.en, Self.zh)
        case .auto:  return isZh ? (Self.zh, Self.en) : (Self.en, Self.zh)
        }
    }
}

extension Character {
    var isCJK: Bool {
        guard let v = unicodeScalars.first?.value else { return false }
        return (0x4E00...0x9FFF).contains(v) || (0x3000...0x303F).contains(v) || (0xFF00...0xFFEF).contains(v)
    }
}

struct ContentView: View {
    @ObservedObject var vm: TranslateViewModel

    var body: some View {
        NavigationStack {
            VStack(spacing: 12) {
                Picker("方向", selection: $vm.direction) {
                    ForEach(TranslateViewModel.Direction.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)

                VStack(alignment: .leading, spacing: 6) {
                    Text("输入").font(.caption).foregroundStyle(.secondary)
                    TextEditor(text: $vm.input)
                        .font(.body)
                        .frame(minHeight: 140)
                        .padding(6)
                        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 12))
                        .overlay(alignment: .topTrailing) {
                            if vm.input.isEmpty {
                                Text("粘贴即译 · 全程本地").font(.footnote).foregroundStyle(.tertiary).padding(12)
                                    .allowsHitTesting(false)
                            }
                        }
                }

                Button {
                    Task { await vm.translate() }
                } label: {
                    HStack {
                        if vm.isTranslating { ProgressView().controlSize(.small) }
                        Text(vm.isTranslating ? "本地翻译中…" : "翻译")
                            .fontWeight(.semibold)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(vm.isTranslating)

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("译文").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        if let ms = vm.elapsed {
                            Text(String(format: "本地 · %.2fs", ms)).font(.caption2).monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                    }
                    ScrollView {
                        Text(vm.output.isEmpty ? " " : vm.output)
                            .font(.body)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
                    }
                    .frame(minHeight: 140)
                    if !vm.output.isEmpty {
                        HStack {
                            Button("复制") {
                                #if canImport(UIKit)
                                UIPasteboard.general.string = vm.output
                                #endif
                            }
                            Button("朗读") { SpeechReader.shared.speak(vm.output, useChineseVoice: vm.speakingIsChinese) }
                            Spacer()
                        }
                        .font(.footnote)
                    }
                }

                if let err = vm.error {
                    Text(err).font(.footnote).foregroundStyle(.red)
                }
                Spacer()
            }
            .padding()
            .navigationTitle("本地翻译")
            .toolbar {
                ToolbarItem(placement: .status) {
                    Text("引擎：\(vm.engineName)").font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}
