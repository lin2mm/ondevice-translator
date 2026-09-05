#include "llama.h"
int main() {
    llama_model_params mp = llama_model_default_params();
    mp.use_mmap = true; mp.use_lock = true;
    return llama_sampler_init_penalties(0, 1.05f, 0.f, 0.f) != nullptr;
}
