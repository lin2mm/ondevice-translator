// ObjC++ bridge. 故意用 ObjC++ 而不是 Swift/C++ interop：后者要对整条依赖树
// 递归设置 interoperabilityMode(.Cxx)，Xcode 里漏一处就报 "cannot find C function"；
// .h/.mm + bridging header 是零配置。Swift 侧只看到这个纯 ObjC 接口。
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

@interface LTSampling : NSObject
@property(nonatomic) float temperature;
@property(nonatomic) float topP;
@property(nonatomic) int topK;
@property(nonatomic) float repeatPenalty;
+ (instancetype)defaults;   // 0.7 / 0.6 / 20 / 1.05  （模型卡推荐）
+ (instancetype)retry;      // 0.3 / 0.8 / 40 / 1.08  （体检失败后的降档重试）
@end

@interface LTLlamaBridge : NSObject

// GGUF 里没有 tokenizer.chat_template（两个档都实测确认），模板必须由我们提供；
// 内容 = tencent/Hy-MT2-1.8B/chat_template.jinja 在无 system prompt 时的展开结果，
// 实现在 .mm 里（含 BOS 与结束 token 名）。
+ (NSString *)chatTemplate;

// 120020 = Hunyuan 的结束 token；llama_vocab_is_eog 会漏，必须自己判
+ (int)eosTokenId;

+ (NSString *)backendInfo;   // 日志用：确认 Metal/GPU 后端真的在（没在就是慢了 3-5 倍）

- (BOOL)loadModelAtPath:(NSString *)path
                 nCtx:(int)nCtx
              threads:(int)threads
            gpuLayers:(int)gpuLayers
                error:(NSError **)error;

- (BOOL)isReady;
- (void)unload;
- (void)requestCancel;

// onToken 会被反复调用（节流后），用于逐字上屏。返回 nil 表示出错。
- (nullable NSString *)translateUserText:(NSString *)userText
                            maxNewTokens:(int)maxNewTokens
                               sampling:(LTSampling *)sampling
                                onToken:(nullable void (^)(NSString *partial))onToken
                                  error:(NSError **)error;

@end

NS_ASSUME_NONNULL_END
