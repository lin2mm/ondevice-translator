package dev.localmt.engine

import android.content.ComponentCallbacks2
import android.content.Context
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.max

/**
 * Engine 协议 = core/engine.py 的 Engine（同一语义，两端可互相对拍）。
 */
interface TranslationEngine {
    val id: String
    val isReady: Boolean
    suspend fun prepare()
    suspend fun generate(
        prompt: PromptSpec,
        maxNewTokens: Int,
        sampling: SamplingSpec,
        onToken: ((String) -> Unit)? = null,
        cancelled: AtomicBoolean,
    ): String
    fun unload()
}

data class PromptSpec(val user: String, val templateId: String = "default")
data class SamplingSpec(
    val temperature: Float = 0.7f,
    val topP: Float = 0.6f,
    val topK: Int = 20,
    val repeatPenalty: Float = 1.05f,
) {
    companion object {
        /** 重试档（core/prompt.py: HYMT2_SAMPLING_SAFE） */
        val SAFE = SamplingSpec(0.3f, 0.8f, 40, 1.08f)
    }
}

class EngineUnavailableException(msg: String, cause: Throwable? = null) : Exception(msg, cause)

/**
 * llama.cpp 后端。
 *
 * 构建要求（见 docs/04）：
 *  - 只发 arm64-v8a；`.so` 必须 `-Wl,-z,max-page-size=16384`（16KB 页设备）
 *  - `android:extractNativeLibs="false"` + 不压缩打包
 *  - 权重 mmap，**不进 Java 堆** → 不需要 largeHeap
 *  - `onTrimMemory` 必须能立刻卸载，否则低内存机上一进后台就被杀
 */
class HyMTLlamaEngine(
    private val context: Context,
    private val modelFile: File,
    private val ctxTokens: Int = 2048,
    private val threads: Int = 4,
) : TranslationEngine, ComponentCallbacks2 {

    override var isReady = false
        private set
    override val id = "hy-mt2-llama-cpp"

    private var handle: Long = 0L
    private val busy = AtomicBoolean(false)
    private val cancel = AtomicBoolean(false)
    private var effectiveCtx = ctxTokens

    init { context.applicationContext.registerComponentCallbacks(this) }

    private external fun nativeLoad(path: String, nCtx: Int, nThreads: Int, gpuLayers: Int): Long
    private external fun nativeGenerate(h: Long, userText: String, maxNew: Int,
                                        temp: Float, topP: Float, topK: Int, repPen: Float,
                                        partial: String, chunkLen: Int): String
    private external fun nativeFree(h: Long)
    private external fun nativeBackendInfo(): String

    override suspend fun prepare() = withContext(Dispatchers.Default) {
        if (isReady) return@withContext
        if (!modelFile.exists()) throw EngineUnavailableException("权重未下载：${modelFile.name}")
        // 自检：Metal/GPU 没生效时日志里不含 "GPU"，这条日志是排查"慢得莫名其妙"的第一现场
        Log.i(TAG, "backend: ${runCatching { nativeBackendInfo() }.getOrElse { "n/a" }}")
        handle = nativeLoad(modelFile.absolutePath, effectiveCtx, threads, 999)
        if (handle == 0L) {
            // 常见原因：1) 当前 llama 版本不认该量化类型 2) ctx 太大分配失败 3) 16KB 对齐问题
            if (effectiveCtx > 1024) {
                Log.w(TAG, "load failed at ctx=$effectiveCtx, retry 1024")
                effectiveCtx = 1024
                handle = nativeLoad(modelFile.absolutePath, effectiveCtx, threads, 999)
            }
            if (handle == 0L) throw EngineUnavailableException("模型加载失败（检查量化格式支持与内存）")
        }
        isReady = true
        Log.i(TAG, "loaded ${modelFile.name} ctx=$effectiveCtx")
    }

    override suspend fun generate(
        prompt: PromptSpec, maxNewTokens: Int, sampling: SamplingSpec,
        onToken: ((String) -> Unit)?, cancelled: AtomicBoolean,
    ): String = withContext(Dispatchers.Default) {
        if (!isReady) prepare()
        while (busy.get()) { kotlinx.coroutines.delay(10) }   // 单实例串行；并行只会更慢更热
        busy.set(true)
        cancel.set(false)
        try {
            // partial/chunkLen 只为让 native 侧能实现"边生成边回调"；无回调时传空
            val out = nativeGenerate(
                handle, prompt.user, maxNewTokens,
                sampling.temperature, sampling.topP, sampling.topK, sampling.repeatPenalty,
                onToken?.let { "" } ?: "", if (onToken == null) 0 else 24,
            )
            out.trim()
        } finally {
            busy.set(false)
        }
    }

    override fun unload() {
        if (handle != 0L) {
            runCatching { nativeFree(handle) }
            handle = 0L
            isReady = false
            Log.i(TAG, "unloaded (memory pressure or idle)")
        }
    }

    fun cancelInFlight() { cancel.set(true) }

    /** 空闲就交还内存：6GB 级设备上"常驻模型"是负资产（后台被杀 + 耗电差评）。 */
    fun scheduleAutoUnload(afterMs: Long = 60_000) {
        Thread {
            val t0 = System.currentTimeMillis()
            while (System.currentTimeMillis() - t0 < afterMs) {
                if (!busy.get() && isReady) {
                    Thread.sleep(500)
                    if (!busy.get()) { unload(); return }
                }
                Thread.sleep(200)
            }
        }.start()
    }

    // ---------- ComponentCallbacks2 ----------
    override fun onTrimMemory(level: Int) {
        when {
            level >= TRIM_MEMORY_BACKGROUND -> unload()
            level >= TRIM_MEMORY_RUNNING_LOW -> {
                effectiveCtx = max(1024, effectiveCtx / 2)
                Log.w(TAG, "trim=$level -> ctx 降到 $effectiveCtx")
            }
        }
    }

    override fun onLowMemory() = unload()

    companion object {
        private const val TAG = "LocalMT/Engine"
        @Volatile private var loaded = false
        fun ensureLibs() {
            if (loaded) return
            synchronized(this) {
                if (!loaded) {
                    // 顺序有讲究：ggml 必须先于 llama（符号依赖）
                    System.loadLibrary("ggml-base")
                    runCatching { System.loadLibrary("ggml-cpu") }
                    runCatching { System.loadLibrary("llama") }
                    System.loadLibrary("localmt-jni")
                    loaded = true
                }
            }
        }
    }
}
