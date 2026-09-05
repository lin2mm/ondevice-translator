plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "dev.localmt"
    compileSdk = 35

    defaultConfig {
        applicationId = "dev.localmt"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1-stage2"
        ndk { abiFilters += "arm64-v8a" }        // 只发 arm64：32 位进程装不下 KV cache
    }

    androidResources {
        // 关键：.gguf 必须不压缩（压缩后无法 mmap，且会在解压时爆内存/耗时）
        noCompress += listOf("gguf", "mnn", "tflite", "litertlm", "bin")
    }
    bundle { language { enableSplit = false } }  // 资产不参与 split，避免"装完没模型"
    val nativeOn = file("../../vendor/llama.cpp").exists()
    packaging {
        jniLibs {
            useLegacyPackaging = false        // true = 解压安装，16KB 页设备上更稳
            // 只在"没有 native"的包里排掉，否则 C 档包会带上一个没人调用的 so
            if (!nativeOn) excludes += "**/libllama.so"
        }
    }
    lint { abortOnError = false }
    buildTypes {
        debug { isMinifyEnabled = false }        // debug 签名 → 可直接侧载，不需要任何账号
        release { isMinifyEnabled = false }
    }
    // native 构建（llama.cpp）只在 vendor 存在时启用；CI 里先 --no-native 出"系统兜底版"
    if (nativeOn) {
        externalNativeBuild { cmake { path = file("src/main/cpp/CMakeLists.txt"); version = "3.31.0" } }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("androidx.work:work-runtime-ktx:2.9.1")
    // C 档兜底（可选）：经典 ML Kit 离线翻译，每语言约 30MB
    implementation("com.google.mlkit:translate:17.0.3")
}
