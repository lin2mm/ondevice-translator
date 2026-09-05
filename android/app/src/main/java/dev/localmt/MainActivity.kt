package dev.localmt

import android.app.Activity
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import dev.localmt.model.ModelStore
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * 阶段 2 最小可跑界面（不用 Compose：少一层依赖 = CI 更容易一次过）。
 * 行为规格在 core/engine.py；这里只做"输入 → 翻译 → 展示 + 采纳反馈"。
 */
class MainActivity : Activity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var store: ModelStore

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = ModelStore(this)

        val pad = (16 * resources.displayMetrics.density).toInt()
        val root = ScrollView(this).apply { setContentPadding(pad, pad, pad, pad) }
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        val status = TextView(this).apply { text = "准备中…" ; setPadding(0, pad / 2, 0, pad / 2) }
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            hint = "粘贴即译 · 全程本地"
            minLines = 4
        }
        val go = Button(this).apply { text = "翻译（本地）" }
        val out = TextView(this).apply {
            setTextIsSelectable(true)
            setTypeface(Typeface.DEFAULT)
            textSize = 17f
            setPadding(0, pad / 2, 0, pad / 2)
        }
        col.addView(status); col.addView(input); col.addView(go); col.addView(out)
        root.addView(col)
        setContentView(root)

        // 选区取词（合规路线，不用 AccessibilityService）：ACTION_PROCESS_TEXT
        val picked = intent?.getStringExtra(android.content.Intent.EXTRA_PROCESS_TEXT)
        if (!picked.isNullOrEmpty()) { input.setText(picked); input.setSelection(picked.length) }
        go.setOnClickListener {
            val text = input.text.toString().trim()
            if (text.isEmpty()) return@setOnClickListener
            status.text = "加载模型…"
            scope.launch {
                val st = store.ensureReady(preferQuality = false)   // 默认吃最小的内置档
                status.text = "翻译中…"
                out.text = "【阶段2 接线点】内置权重已就绪：$st\n把 core/engine.py 的 Translator 移植到 Kotlin 后，这里调用 translate(text)。"
                Toast.makeText(this@MainActivity, "模型状态：${st::class.simpleName}", Toast.LENGTH_LONG).show()
            }
        }
    }

}
