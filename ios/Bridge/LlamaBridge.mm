#import "LlamaBridge.h"
#include <atomic>
#include <cstring>
#include <vector>
#include <sys/sysctl.h>
#include "llama.h"
#include "ggml-backend.h"   // +backendInfo 用到 ggml_backend_dev_*，不 include 会编译失败

// 实测事实（2026-09，直接读 tencent/Hy-MT2-1.8B 的 config / tokenizer_config / chat_template.jinja
// 与两个 GGUF 的文件头）：
//   general.architecture = hunyuan-dense；32 层 / emb 2048 / kv_head 4 / vocab 120818
//   GGUF **没有** tokenizer.chat_template → 模板必须自己给（chatTemplate / renderedUser）
//   bos = 120000(hy_begin▁of▁sentence)、eos = 120020(hy_place▁holder▁no▁2)、pad = 120002
//   context_length = 262144 → **绝不能用它**，n_ctx 必须钉 2048/1024
//   KV：2 × 32 × 4 × 128 × 2B = 64KB/token → 2048 ctx ≈ 131MB
//
// 特殊 token 的字面量用 unicode 组装（< + | + 名 + | + >）：直写在某些编辑器/工具链里会被吞，
// 且 Hunyuan 系改过 token 命名，集中一处好改。
// 特殊 token 的竖线**必须是全角 U+FF5C**（实测：真实 GGUF 前 3MB 里 ASCII `<|hy` 出现 0 次、
// `<｜hy` 出现 803 次；tokenizer_config 的 bos/eos 也是全角）。写成 ASCII 竖线 → 词表里查不到
// → 被当普通文本分词 → 开头复读 + 停不下来。这里刻意用 \uFF5C 转义，防止任何编辑器/复制环节被规范化。
static NSString *const kOp = @"<\uFF5C";
static NSString *const kCl = @"\uFF5C>";
static NSString *LTWrap(NSString *name) { return [NSString stringWithFormat:@"%@%@%@", kOp, name, kCl]; }
static NSString *const kBOS = @"hy_begin▁of▁sentence";
static NSString *const kUSR = @"hy_User";
static NSString *const kAST = @"hy_Assistant";
static NSString *const kEOS = @"hy_place▁holder▁no▁2";

@implementation LTSampling
// 采样值必须与 core/prompt.py 的 HYMT2_SAMPLING 逐字一致（模型卡正文 0.6；
// 权重内嵌 general.sampling.top_p 实测 0.8 —— 已决定以 core 为准，见 docs/06 风险条目）。
+ (instancetype)defaults { LTSampling *s = [LTSampling new]; s.temperature = 0.7f; s.topP = 0.6f; s.topK = 20; s.repeatPenalty = 1.05f; return s; }
+ (instancetype)retry    { LTSampling *s = [LTSampling new]; s.temperature = 0.3f; s.topP = 0.8f; s.topK = 40; s.repeatPenalty = 1.08f; return s; }
@end

@implementation LTLlamaBridge {
    llama_model *_model;
    llama_context *_ctx;
    int _nCtx;
    std::atomic<bool> _cancel;
}

+ (NSString *)chatTemplate {
    // 与 renderedUser: 同源，避免两处模板漂移（jinja 无 system 分支的展开 = BOS + user 轮 + assistant 头）
    return [self renderedUser:@""];
}
+ (int)eosTokenId { return 120020; }

+ (NSString *)renderedUser:(NSString *)text {
    // 结尾必须补 assistant 头，否则模型不知道"该我说了"。
    // 注意：stringWithFormat 的占位符数量以前写成 3 个却传 4 个参数 —— kAST 被静默丢掉，
    // 表现就是"输出正常但经常不答"。这里必须 4 个 %@。
    return [NSString stringWithFormat:@"%@\n%@\n%@\n%@", LTWrap(kBOS), LTWrap(kUSR), text, LTWrap(kAST)];
}

+ (NSString *)backendInfo {
    NSMutableString *s = [NSMutableString string];
    for (size_t i = 0; i < ggml_backend_dev_count(); ++i)
        [s appendFormat:@"%s;", ggml_backend_dev_name(ggml_backend_dev_get(i))];
    return s;   // 里面必须有 METAL，否则你在跑 CPU（慢 3-5 倍且无报错）
}

static NSError *LTErr(NSInteger c, NSString *m) {
    return [NSError errorWithDomain:@"LTLlama" code:c userInfo:@{NSLocalizedDescriptionKey: m}];
}
static int64_t LTMemsize(void) {
    int64_t m = 0; size_t sz = sizeof(m);
    sysctlbyname("hw.memsize", &m, &sz, NULL, 0);
    return m;
}

- (BOOL)isReady { return _ctx != nullptr; }

- (BOOL)loadModelAtPath:(NSString *)path nCtx:(int)nCtx threads:(int)threads
              gpuLayers:(int)gpuLayers error:(NSError **)error {
    if (_ctx) return YES;
    if (![[NSFileManager defaultManager] fileExistsAtPath:path]) {
        if (error) *error = LTErr(1, @"权重不在 App 包内（跑 install-on-iphone15.sh --model small 重新打包）");
        return NO;
    }
    if (LTMemsize() > 0 && LTMemsize() < 4LL * 1000 * 1000 * 1000) { nCtx = 1024; gpuLayers = 0; }  // 老机器降级
    llama_model_params mp = llama_model_default_params();
    // v0.4.0 起 use_mmap/use_mlock 字段已从 llama_model_params 删除，改由 load_mode 表达
    // （实测 v0.4.0 include/llama.h：字段只有 load_mode/lazy_mode 等，写 use_mmap 直接编译失败）。
    //   MMAP = 内存映射，内核可回收 page cache（462MB 不变纯 RSS）；
    //   刻意不用 MMAP_MLOCK：mlock 会把 462MB 钉死在物理内存，6GB 机器上是负资产。
    mp.load_mode = LLAMA_LOAD_MODE_MMAP;
    mp.n_gpu_layers = gpuLayers;
    _model = llama_model_load_from_file(path.UTF8String, mp);
    if (!_model) { if (error) *error = LTErr(2, @"加载失败：确认 llama.cpp 支持 hunyuan-dense 与该量化类型"); return NO; }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = nCtx;                 // 别用权重里的 262144
    cp.n_batch = 512;
    cp.n_threads = threads;
    cp.n_threads_batch = threads;
    _ctx = llama_new_context_with_model(_model, cp);
    if (!_ctx) { llama_model_free(_model); _model = nullptr; if (error) *error = LTErr(3, @"ctx 分配失败，降 n_ctx 到 1024"); return NO; }
    _nCtx = nCtx; _cancel.store(false);
    return YES;
}

- (void)unload { if (_ctx) { llama_free(_ctx); _ctx = nullptr; } if (_model) { llama_model_free(_model); _model = nullptr; } }
- (void)requestCancel { _cancel.store(true); }
- (void)dealloc { [self unload]; }

- (NSString *)translateUserText:(NSString *)userText maxNewTokens:(int)maxNew sampling:(LTSampling *)sp
                       onToken:(void (^)(NSString *))onToken error:(NSError **)error {
    if (!_ctx) { if (error) *error = LTErr(4, @"模型未加载"); return nil; }
    _cancel.store(false);

    NSString *rendered = [[self class] renderedUser:userText];
    const llama_vocab *vocab = llama_model_get_vocab(_model);
    std::vector<int32_t> ids(rendered.length + 8);
    int n = llama_tokenize(vocab, rendered.UTF8String, (int)strlen(rendered.UTF8String),
                           ids.data(), (int)ids.size(), /*add_special*/ false, /*parse_special*/ true);
    if (n <= 0) { if (error) *error = LTErr(5, @"分词失败"); return nil; }
    if (n + maxNew > _nCtx) {
        if (error) *error = LTErr(6, [NSString stringWithFormat:@"输入过长（%d token / ctx %d）—— 请分段，别加大 ctx", n, _nCtx]);
        return nil;                     // 显式拒绝比 KV 溢出后静默乱码好
    }

    llama_sampler *smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(sp.temperature));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(sp.topK));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(sp.topP, 1));
    // v0.4.0 签名变为 (n_vocab, penalty_last_n, penalty_repeat, penalty_freq, penalty_present)：
    // 旧的 4 参调用编译不过；且新 API 里 penalty_last_n=0 是"关闭惩罚"，必须显式给 64。
    // n_vocab 用 llama_vocab_n_tokens(vocab) 实测值（llama_n_vocab 已弃用）。
    llama_sampler_chain_add(smpl,
        llama_sampler_init_penalties(llama_vocab_n_tokens(vocab), 64, sp.repeatPenalty, 0.f, 0.f));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

    llama_memory_clear(llama_get_memory(_ctx), false);
    llama_batch batch = llama_batch_get_one(ids.data(), n);
    if (llama_decode(_ctx, batch) != 0) {
        llama_sampler_free(smpl);
        if (error) *error = LTErr(7, @"prefill 失败（超出 n_ctx，或该量化不被支持）");
        return nil;
    }

    NSMutableString *out = [NSMutableString string];
    std::vector<char> piece(1024);
    NSString *eosStr = LTWrap(kEOS);
    for (int i = 0; i < maxNew; ++i) {
        if (_cancel.load()) break;
        llama_token id = llama_sampler_sample(smpl, _ctx, -1);
        // 双重停止判断：llama_vocab_is_eog 在这种自定义 special token 上会漏
        if (llama_vocab_is_eog(vocab, id) || (int)id == [[self class] eosTokenId]) break;
        int len = llama_token_to_piece(vocab, id, piece.data(), (int)piece.size(), 0, true);
        if (len > 0) {
            NSString *s = [[NSString alloc] initWithBytes:piece.data() length:len encoding:NSUTF8StringEncoding];
            if (s.length) {
                [out appendString:s];
                if (onToken && (i % 6 == 0)) onToken(out);
                if ([out containsString:eosStr] || [out containsString:LTWrap(kUSR)] || [out containsString:@"###"]) break;
            }
        }
        llama_token one = id;
        if (llama_decode(_ctx, llama_batch_get_one(&one, 1)) != 0) break;
    }
    llama_sampler_free(smpl);
    // 收尾清洗：把可能混进来的结束符/下一轮提示截掉
    NSString *final = out;
    for (NSString *cut in @[eosStr, LTWrap(kUSR), LTWrap(kAST), @"###"]) {
        NSRange r = [final rangeOfString:cut];
        if (r.location != NSNotFound) final = [final substringToIndex:r.location];
    }
    if (onToken) onToken(final);
    return [final stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
}

@end
