// 编译级探针：逐一使用 LlamaBridge.mm（补丁后）调用的全部 llama.cpp v0.4.0 API。
// 在 Linux 上用 g++ -fsyntax-only 编译通过 = 这些符号/字段/签名在真实 v0.4.0 头文件中
// 全部存在且类型匹配（比 grep 更强的证据；无法验证的是 iOS/Metal 特有行为，需 Mac 实测）。
#include "llama.h"
#include "ggml-backend.h"
#include <cstdint>
#include <cstring>
#include <vector>

static void probe(const char *path, const char *text) {
    // ---- loadModelAtPath（补丁后）----
    llama_model_params mp = llama_model_default_params();
    mp.n_gpu_layers = 999;
    mp.load_mode = LLAMA_LOAD_MODE_MMAP;              // v0.4.0: use_mmap/use_lock 已删除
    llama_model *model = llama_model_load_from_file(path, mp);
    if (!model) return;

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = 2048; cp.n_batch = 512;
    cp.n_threads = 4; cp.n_threads_batch = 4;
    llama_context *ctx = llama_new_context_with_model(model, cp);  // 弃用但仍存在
    if (!ctx) { llama_model_free(model); return; }

    // ---- translateUserText（补丁后）----
    const llama_vocab *vocab = llama_model_get_vocab(model);
    std::vector<int32_t> ids(4096);
    int n = llama_tokenize(vocab, text, (int)strlen(text), ids.data(), (int)ids.size(),
                           /*add_special*/ false, /*parse_special*/ true);
    if (n <= 0) { llama_free(ctx); llama_model_free(model); return; }

    llama_sampler *smpl = llama_sampler_chain_init(llama_sampler_chain_default_params());
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(0.7f));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(20));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(0.6f, 1));
    // v0.4.0 五参签名（原 4 参调用编译失败的原因）
    llama_sampler_chain_add(smpl,
        llama_sampler_init_penalties(llama_vocab_n_tokens(vocab), 64, 1.05f, 0.f, 0.f));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

    llama_memory_clear(llama_get_memory(ctx), false);
    llama_batch batch = llama_batch_get_one(ids.data(), n);
    if (llama_decode(ctx, batch) == 0) {
        char piece[1024];
        for (int i = 0; i < 16; ++i) {
            llama_token id = llama_sampler_sample(smpl, ctx, -1);
            if (llama_vocab_is_eog(vocab, id) || (int)id == 120020) break;   // 自定义 EOS
            int len = llama_token_to_piece(vocab, id, piece, (int)sizeof(piece), 0, true);
            if (len <= 0) break;
            llama_token one = id;
            if (llama_decode(ctx, llama_batch_get_one(&one, 1)) != 0) break;
        }
    }
    llama_sampler_free(smpl);
    llama_free(ctx);
    llama_model_free(model);
}

// ---- backendInfo ----
static const char *probe_backends() {
    static char buf[512]; size_t o = 0;
    for (size_t i = 0; i < ggml_backend_dev_count(); ++i)
        o += (size_t)snprintf(buf + o, sizeof(buf) - o, "%s;", ggml_backend_dev_name(ggml_backend_dev_get(i)));
    return buf;
}

int main() { probe("/dev/null", "<|hy_User|>\nhi\n<|hy_Assistant|>\n"); (void)probe_backends(); return 0; }
