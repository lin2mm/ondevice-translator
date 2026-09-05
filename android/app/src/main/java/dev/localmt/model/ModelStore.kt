package dev.localmt.model

import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * 权重下载 + 校验 + 磁盘预算 + 降级选档。
 * 契约来自 core/model_manifest.py（同一份 manifest.json 喂两端）。
 *
 * 三条不容妥协的规则：
 *  1. 权重放 filesDir/models，且在 backup_rules.xml 里 exclude —— 否则 462MB 进 Auto Backup。
 *  2. 校验通过前不 rename —— App 只认 sha256 匹配的正式文件。
 *  3. 空间不足不是错误，是"换档"：按体积从小到大找能装下的包（对应 Manifest.downgrade_chain）。
 */
class ModelStore(private val context: Context) {

    sealed interface State {
        data object Idle : State
        data object Checking : State
        data class Queued(val reason: String) : State
        data class Downloading(val fraction: Float, val bytesPerSec: Long) : State
        data class Verifying(val fraction: Float) : State
        data class Ready(val assetId: String, val file: File, val engineHint: String) : State
        data class Failed(val message: String, val retryable: Boolean = true) : State
    }

    data class Asset(
        val id: String, val url: String, val sha256: String, val sizeBytes: Long,
        val quant: String, val minRamBytes: Long, val minFreeBytes: Long,
        val minAndroidApi: Int, val engine: String, val license: String, val tier: Int,
    )

    private val dir = File(context.filesDir, "models").apply { mkdirs() }
    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private val _state = MutableStateFlow<State>(State.Idle)
    val state: StateFlow<State> = _state

    // ---------- manifest ----------

    fun loadManifest(): List<Asset> {
        val json = context.assets.open("manifest.json").bufferedReader().use { it.readText() }
        val root = JSONObject(json)
        val arr = root.getJSONArray("assets")
        return (0 until arr.length()).map { i ->
            val o = arr.getJSONObject(i)
            Asset(
                id = o.getString("id"),
                url = o.getString("url"),
                sha256 = o.getString("sha256"),
                sizeBytes = o.getLong("size_bytes"),
                quant = o.optString("quant", "unknown"),
                minRamBytes = o.optLong("min_ram_bytes", 0),
                minFreeBytes = o.optLong("min_free_bytes", 0),
                minAndroidApi = o.optInt("min_android_api", 28),
                engine = o.optString("engine", "llama-cpp"),
                license = o.optString("license", ""),
                tier = o.optInt("tier", 1),
            )
        }.also { list ->
            // 与 Python 侧一致的发布前校验（这里做运行期兜底，防止线上 manifest 写错）
            list.forEach { a ->
                require(a.sha256.length == 64 && a.sha256.all { it in "0123456789abcdef" }) {
                    "asset ${a.id}: sha256 非法"
                }
                require(a.sizeBytes > 1_000_000) { "asset ${a.id}: size_bytes 可疑" }
                require(a.minFreeBytes >= a.sizeBytes * 105 / 100) { "asset ${a.id}: 磁盘余量门槛低于体积" }
            }
        }
    }

    /** 物理内存：ActivityManager.memoryInfo.totalMem（不是 Runtime.maxMemory，后者只是 Java 堆上限）。 */
    private fun deviceRamBytes(): Long {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as android.app.ActivityManager
        val info = android.app.ActivityManager.MemoryInfo()
        am.getMemoryInfo(info)
        return info.totalMem.toLong()
    }

    fun selectAsset(preferQuality: Boolean = true): Asset? {
        val ram = deviceRamBytes()
        val api = android.os.Build.VERSION.SDK_INT
        val free = dir.usableSpace
        val candidates = loadManifest().filter {
            free >= it.minFreeBytes && ram >= it.minRamBytes && api >= it.minAndroidApi
        }
        return if (preferQuality) candidates.maxByOrNull { it.sizeBytes } else candidates.minByOrNull { it.sizeBytes }
    }

    fun installed(asset: Asset): File? {
        val f = File(dir, "${asset.id}.gguf")
        return if (f.length() == asset.sizeBytes) f else null
    }

    // ---------- 下载 ----------

    fun enqueueDownload(assetId: String, requireWifi: Boolean = true) {
        val net = if (requireWifi) NetworkType.UNMETERED else NetworkType.CONNECTED
        val req = OneTimeWorkRequestBuilder<ModelDownloadWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(net).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .setInputData(Data.Builder().putString("asset_id", assetId).build())
            .addTag(TAG)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork("dl-$assetId", ExistingWorkPolicy.KEEP, req)
    }

    suspend fun ensureReady(preferQuality: Boolean = true): State {
        val asset = selectAsset(preferQuality)
            ?: return State.Failed("当前设备内存/存储空间不足以运行本地翻译，已回落到系统翻译", retryable = false)
        installed(asset)?.let {
            return State.Ready(asset.id, it, asset.engine).also { s -> _state.value = s }
        }
        _state.value = State.Checking
        return runCatching { downloadAndVerify(asset) }
            .getOrElse {
                Log.w(TAG, "download failed", it)
                State.Failed("下载失败：${it.message}").also { s -> _state.value = s }
            }
    }

    /** internal：Worker 直接调用（同一份实现，避免下载逻辑出现两个版本）。 */
    internal suspend fun downloadAndVerify(asset: Asset): State = withContext(Dispatchers.IO) {
        val part = File(dir, "${asset.id}.gguf.part")
        val start = part.length()
        if (start > asset.sizeBytes) part.delete()

        val req = Request.Builder().url(asset.url).apply {
            if (part.exists() && part.length() in 1 until asset.sizeBytes) {
                header("Range", "bytes=${part.length()}-")
            }
        }.build()

        http.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful && resp.code != 206) {
                return@withContext State.Failed("HTTP ${resp.code} ${resp.message}")
                    .also { _state.value = it }
            }
            val body = resp.body ?: return@withContext State.Failed("空响应体").also { _state.value = it }
            val total = if (resp.code == 206) asset.sizeBytes else body.contentLength().takeIf { it > 0 } ?: asset.sizeBytes
            RandomAccessFile(part, "rw").use { raf ->
                if (resp.code != 206) raf.setLength(0)
                raf.seek(raf.length())
                val src = body.byteStream()
                val buf = ByteArray(1 shl 20)
                var written = raf.length()
                var lastTick = System.currentTimeMillis()
                var lastBytes = written
                while (true) {
                    val n = src.read(buf)
                    if (n <= 0) break
                    raf.write(buf, 0, n)
                    written += n
                    val now = System.currentTimeMillis()
                    if (now - lastTick > 200) {
                        val rate = (written - lastBytes) * 1000L / (now - lastTick).coerceAtLeast(1)
                        lastTick = now; lastBytes = written
                        _state.value = State.Downloading(
                            (written.toFloat() / total.toFloat()).coerceIn(0f, 0.999f), rate
                        )
                    }
                }
            }
        }

        // 体积先行判断，省一次全量 sha256
        if (part.length() != asset.sizeBytes) {
            return@withContext State.Failed("长度不符（${part.length()}/${asset.sizeBytes}），将续传")
                .also { _state.value = it }
        }
        _state.value = State.Verifying(0f)
        val digest = sha256Streaming(part) { frac -> _state.value = State.Verifying(frac) }
        if (!digest.equals(asset.sha256, ignoreCase = true)) {
            // 不无脑重下：先删掉，下次 Range 从头开始，但保留一次重试机会
            part.delete()
            return@withContext State.Failed("sha256 校验失败，已删除损坏副本")
                .also { _state.value = it }
        }
        val final = File(dir, "${asset.id}.gguf")
        if (final.exists()) final.delete()
        if (!part.renameTo(final)) return@withContext State.Failed("rename 失败（目录权限？）").also { _state.value = it }
        State.Ready(asset.id, final, asset.engine).also { _state.value = it }
    }

    private fun sha256Streaming(file: File, onProgress: (Float) -> Unit): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(1 shl 21)
            val total = file.length().coerceAtLeast(1)
            var done = 0L
            while (true) {
                val n = input.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
                done += n
                if (done % (1 shl 25) < n) onProgress(done.toFloat() / total.toFloat())
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    fun isWifi(): Boolean {
        val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val net = cm.activeNetwork ?: return false
        val caps = cm.getNetworkCapabilities(net) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
    }

    companion object { const val TAG = "LocalMT/ModelStore" }
}

/** 后台下载 Worker：断点续传 + 用户可见进度（notification 由 UI 侧订阅 state 渲染）。 */
class ModelDownloadWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val store = ModelStore(applicationContext)
        val id = inputData.getString("asset_id") ?: return Result.failure()
        val asset = runCatching { store.loadManifest().first { it.id == id } }.getOrNull() ?: return Result.failure()
        return when (val st = store.downloadAndVerify(asset)) {
            is ModelStore.State.Ready -> Result.success()
            is ModelStore.State.Failed ->
                if (st.retryable && runAttemptCount < 4) Result.retry() else Result.failure()
            else -> Result.retry()
        }
    }
}
