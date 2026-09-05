# 用 CMakeLists 而不是 ndk-build：llama.cpp 官方就是 CMake，接进来最省事。
# 位置：app/src/main/cpp/CMakeLists.txt —— 只有 app/build.gradle.kts 检测到
# ../../vendor/llama.cpp 存在时才会被引用（没有 vendor 时 = C 档包，纯系统兜底）。
cmake_minimum_required(VERSION 3.22.1)
project(localmt_jni CXX)

# cpp → main → src → app → android → 仓库根，一共 5 层（写成 4 层会指到 android/vendor）
set(LLAMA_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../../../../vendor/llama.cpp)

# 关键 1：16KB 页。缺了它，Android 15+ 的 Pixel/新机上 dlopen 直接失败，
# 报错长得像"找不到符号"，非常难查（docs/06 风险表里排第一）。
add_link_options("-Wl,-z,max-page-size=16384")

# 关键 2：只编 arm64 + 用到 CPU 特性。x86/arm32 没意义（KV cache 装不下），
# dotprod/fp16 在 A78 之后的核上有明显收益；老机器靠运行时探测，编上不会崩。
set(CMAKE_ANDROID_STL_TYPE c++_shared)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# llama.cpp 侧的开关：Metal 无关（Android 走 CPU/Vulkan），但 mmap 必须开
set(LLAMA_BUILD_TESTS    OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER   OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL        OFF CACHE BOOL "" FORCE)   # 不联网，别把 ssl 拖进包
set(LLAMA_CURL           OFF CACHE BOOL "" FORCE)
set(GGML_LLAMAFILE       ON  CACHE BOOL "" FORCE)   # 解码更快；关掉能再省一点体积

add_subdirectory(${LLAMA_DIR} llama-build)

add_library(localmt-jni SHARED llama_jni.cpp)
target_include_directories(localmt-jni PRIVATE ${LLAMA_DIR}/include ${LLAMA_DIR}/ggml/include)
target_link_libraries(localmt-jni PRIVATE log android llama ggml ggml-base)
target_compile_options(localmt-jni PRIVATE -O3 -fvisibility=hidden -DNDEBUG)

# llama/ggml 作为 imported/static 库也要同一 linker flag，否则只对齐了我们这层
set_target_properties(llama ggml ggml-base PROPERTIES
    INTERFACE_LINK_OPTIONS "-Wl,-z,max-page-size=16384")
