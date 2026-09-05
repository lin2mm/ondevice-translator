// JNI 胶水：Kotlin <-> llama.cpp。对应 CMakeLists 关键项见文件末尾注释。
// 这是"核心骨架 + 容易漏的开关"，落地时按你引入的 llama.cpp 版本对齐 API 名称
// （0.3.x 之后 sampler chain / vocab API 改过名，别照抄老教程）。

#include <jni.h>
#include <atomic>
#include <string>
#include <vector>
#include <android/log.h>
#include "llama.h"

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "LocalMT", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "LocalMT", __VA_ARGS__)

namespace {
struct Engine {
    llama_model *model = nullptr;
    llama_context *ctx = nullptr;
    llama_sampler *sampler = nullptr;
    std::atomic<bool> aborting{false};
    int n_ctx = 2048;
};

// Hunyuan 的特殊 token：**竖线是全角 U+FF5C**，不是 ASCII '|'。
// 实测依据：真实 GGUF 前 3MB 里 ASCII `<|hy` 出现 0 次、`<｜hy` 出现 803 次；
// tokenizer_config.json 的 bos/eos 同样是全角。写成 ASCII 竖线 → 词表查不到 → 当普通文本
// → 开头复读、停不下来。这里一律用 \uFF5C 转义（源码保持 ASCII，任何编辑器都不会把它"顺手规范化"）。
const char *const kTokBOS  = "<\uFF5Chy_begin\u2581of\u2581sentence\uFF5C>";   // id 120000
const char *const kTokUser = "<\uFF5Chy_User\uFF5C>";                            // id 120006
const char *const kTokAsst = "<\uFF5Chy_Assistant\uFF5C>";                       // id 120007
const char *const kTokEOS  = "<\uFF5Chy_place\u2581holder\u2581no\u25812\uFF5C>";// id 120020 = eos_token
const int32_t kEosId = 120020;   // llama_vocab_is_eog 会漏，所以额外按 id 判停

// tencent/Hy-MT2-1.8B/chat_template.jinja 在"无 system、单轮 user、add_generation_prompt=true"
// 时的展开结果。权重里**没有** tokenizer.chat_template（两个档都实测确认），
// 所以不能调 llama_chat_apply_template（它拿不到模板会退回 ChatML 默认值，直接掉质量）。
std::string renderHunyuanPrompt(const std::string &user) {
    std::string s;
    s += kTokBOS;  s += "\n";
    s += kTokUser; s += "\n";
    s += user;     s += "\n";
    s += kTokAsst;                       // 生成时必须补 assistant 头，否则模型不知道"该它答"
    return s;
}
}  // namespace

extern "C" {

JNIEXPORT jlong JNICALL
Java_dev_localmt_engine_HyMTLlamaEngine_nativeLoad(JNIEnv *env, jobject, jstring path_,
                                                   jint n_ctx, jint n_threads, jint gpu_layers) {
    const char *path = env->GetStringUTFChars(path_, nullptr);
    auto *e = new Engine();
    e->n_ctx = n_ctx;

    llama_model_params mp = llama_model_default_params();
    mp.use_mmap = true;             // 必须：让内核能回收 page cache；false 会把 462MB 变成纯 RSS
    mp.use_lock = true;
    mp.n_gpu_layers = gpu_layers;   // 999；无 GPU 后端时 llama.cpp 自动回落 CPU（日志里确认）

    e->model = llama_model_load_from_file(path, mp);
    env->ReleaseStringUTFChars(path_, path);
    if (!e->model) { LOGE("load failed"); delete e; return 0; }

    llama_context_params cp = llama_context_default_params();
    cp.n_ctx = n_ctx;               // 2048 起；内存告警时 Kotlin 侧降 1024 重载
    cp.n_batch = 512;
    cp.n_threads = n_threads;
    cp.n_threads_batch = n_threads;
    e->ctx = llama_new_context_with_model(e->model, cp);
    if (!e->ctx) { llama_model_free(e->model); delete e; return 0; }

    LOGI("loaded ctx=%d threads=%d layers=%d", n_ctx, n_threads, gpu_layers);
    return (jlong)(intptr_t)e;
}

JNIEXPORT jstring JNICALL
Java_dev_localmt_engine_HyMTLlamaEngine_nativeGenerate(JNIEnv *env, jobject, jlong h,
                                                       jstring user_text, jint max_new,
                                                       jfloat temp, jfloat top_p, jint top_k,
                                                       jfloat rep_pen, jstring, jint) {
    auto *e = (Engine *)(intptr_t)h;
    if (!e || !e->ctx) return env->NewStringUTF("");

    const char *user = env->GetStringUTFChars(user_text, nullptr);
    std::string text(user);
    env->ReleaseStringUTFChars(user_text, user);

    // 自己拼 Hunyuan 模板（见 renderHunyuanPrompt 上方注释），并据此关闭 add_special：
    // tokenizer.ggml.add_bos_token = true，如果 add_special=true 就会**双 BOS**，首句概率性复读。
    const std::string prompt = renderHunyuanPrompt(text);

    llama_sampler_chain_params sp = llama_sampler_chain_default_params();
    llama_sampler *smpl = llama_sampler_chain_init(sp);
    llama_sampler_chain_add(smpl, llama_sampler_init_temp(temp));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_k(top_k));
    llama_sampler_chain_add(smpl, llama_sampler_init_top_p(top_p, 1));
    llama_sampler_chain_add(smpl, llama_sampler_init_penalties(0, rep_pen, 0.0f, 0.0f));
    llama_sampler_chain_add(smpl, llama_sampler_init_dist(LLAMA_DEFAULT_SEED));

    const llama_vocab *vocab = llama_model_get_vocab(e->model);
    std::vector<int32_t> toks(prompt.size() + 8);
    int n = llama_tokenize(vocab, prompt.c_str(), (int)prompt.size(), toks.data(), (int)toks.size(),
                           /*add_special*/ false,   // 模板里已经有 BOS，再让引擎加就双 BOS
                           /*parse_special*/ true); // 必须 true，否则全角竖线的 token 会被拆开
    std::string out;
    if (n > 0) {
        if (n + max_new > e->n_ctx) {           // 超长要显式拒绝，不能靠 KV 溢出静默出错
            llama_sampler_free(smpl);
            return env->NewStringUTF("<<ERR_TOO_LONG>>");
        }
        llama_memory_clear(llama_get_memory(e->ctx), false);
        llama_batch batch = llama_batch_get_one(toks.data(), n);
        if (llama_decode(e->ctx, batch) == 0) {
            std::string piece;
            for (int i = 0; i < max_new; ++i) {
                if (e->aborting.load()) break;
                llama_token id = llama_sampler_sample(smpl, e->ctx, -1);
                if (llama_vocab_is_eog(vocab, id) || id == kEosId) break;   // 双保险：实测 is_eog 会漏
                piece.resize(256);
                int len = llama_token_to_piece(vocab, id, piece.data(), 256, 0, true);
                if (len > 0) out.append(piece.data(), len);
                llama_batch one = llama_batch_get_one(&id, 1);
                if (llama_decode(e->ctx, one) != 0) break;
            }
        }
    }
    llama_sampler_free(smpl);
    return env->NewStringUTF(out.c_str());
}

JNIEXPORT void JNICALL
Java_dev_localmt_engine_HyMTLlamaEngine_nativeFree(JNIEnv *, jobject, jlong h) {
    auto *e = (Engine *)(intptr_t)h;
    if (!e) return;
    if (e->sampler) llama_sampler_free(e->sampler);
    if (e->ctx) llama_free(e->ctx);
    if (e->model) llama_model_free(e->model);
    delete e;
}

}  // extern "C"

/* CMakeLists.txt 必须包含（缺第 2 条 = 16KB 页设备 dlopen 失败）：
 *
 *   add_library(localmt-jni SHARED llama_jni.cpp)
 *   target_include_directories(localmt-jni PRIVATE ${LLAMA_DIR}/include)
 *   target_link_libraries(localmt-jni log android llama ggml ggml-base)
 *   target_compile_options(localmt-jni PRIVATE -O3 -fvisibility=hidden)
 *   target_link_options(localmt-jni PRIVATE "-Wl,-z,max-page-size=16384" "-Wl,--build-id=sha1")
 *   # llama/ggml 两个 imported target 也要同样对齐：
 * #  set_target_properties(llama ggml PROPERTIES INTERFACE_LINK_OPTIONS "-Wl,-z,max-page-size=16384")
 *
 * gradle 侧： packaging { jniLibs { useLegacyPackaging = false } } + abiFilters "arm64-v8a"
 * 验证： python3 ../../../../tools/lint_platform_code.py 会扫这个文件里有没有那行 linker flag
 */
