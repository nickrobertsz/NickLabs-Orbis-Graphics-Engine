# AI compute on Liverpool (Graphics Engine 2.0)

## Paths that exist

| Path | Status on the PS4 | How it is checked |
|---|---|---|
| CPU (llama.cpp b11845, `-march=btver2`) | the Orbis 0.2.0 default AI backend | reference for every comparison |
| Vulkan (RADV, llama.cpp Vulkan build in `/usr/lib/orbis/llama/vulkan`) | runs; numerically correct only for short prompts in the community report below | `orbis-ai-gpu campaign` + the 0.2.0 GPU gate |
| OpenCL (RustiCL on radeonsi) | available in Mesa 26.2.5-201 (`RUSTICL_ENABLE=radeonsi`) | `orbis-gpu qualify` (`cl.rusticl` integer oracle) |
| ROCm / HIP | **not possible** — GFX7 has no amdkfd/HSA in this kernel | — |

## The fault to beat

Carlos Georges reported this on a PS4 (2026-09-25): short prompts on Vulkan give exactly the
expected output, but a longer prompt faults the GPU during prefill, and GPU recovery fails. His
mitigations were narrower matrix dispatches and a simpler attention path. Nothing in this release
reproduces it; the campaign below is built to find the boundary on Nick's console and a
configuration that stays below it, or to prove that none exists.

## Campaign (`orbis-ai-gpu`)

Each variant states one hypothesis and uses only environment knobs that exist in llama.cpp b11845
(all verified in `ggml-vulkan.cpp` at 9ff0d7e):

| Variant | Setting | Hypothesis |
|---|---|---|
| baseline | ubatch 512 | reproduce |
| ub128 / ub64 | smaller prefill micro-batch | wide matmul dispatches fault |
| nodes32 | `GGML_VK_MAX_NODES_PER_SUBMIT=32` | long command buffers / GPU watchdog |
| serialize | `GGML_VK_SERIALIZE_SUBMISSIONS=1` | overlapping submissions |
| hostmem | `GGML_VK_PREFER_HOST_MEMORY=1` | carve-out exhaustion / VRAM placement |
| maxalloc | `GGML_VK_FORCE_MAX_ALLOCATION_SIZE` / `_MAX_BUFFER_SIZE` = 512 MiB | large buffers / GTT limits |
| nof16 | `GGML_VK_DISABLE_F16=1` | fp16 path |
| nofusion | `GGML_VK_DISABLE_FUSION=1` | fused shaders |
| nommvq | `GGML_VK_DISABLE_MMVQ=1` + `GGML_VK_DISABLE_INTEGER_DOT_PRODUCT=1` | quantized mat-vec / int dot paths |
| ub64-hostmem-nodes32 | combination | several at once |

Every variant runs through the Orbis 0.2.0 inference harness (`orbis-infer-harness`):
* contexts 128…4096 with bisection to the first failing size;
* KL divergence against the CPU build on the same tokens (thresholds: mean KLD ≤ 0.01, same top-1
  ≥ 99 %, KLD p99 ≤ 0.1);
* intermediate-tensor comparison (`llama-eval-callback`), which names the first diverging tensor;
* a mixed-length server check for greedy-output identity;
* kernel-log capture per step and an fsync'd journal, so a hang plus power cycle resumes where it
  stopped (`DONE` markers per variant);
* optional mirroring to the Nobara (`--remote-log udp://<IP>:47999`, received with
  `orbis-infer-harness receive`), so a console hang still leaves a trail.

`orbis-ai-gpu recommend` scores every variant, preferring:
1. the longest numerically correct prefill;
2. correct tensors;
3. speed at that length.

`use_vulkan` is true only if that winner also has correct intermediate tensors. `orbis-ai-gpu
apply` then writes the variant's environment into
`orbis-llama.service.d/90-orbis-graphics-engine.conf`, sets `orbis ai set ubatch`, and runs the
harness gate. The gate requires short and long prefill (1024 and 2048), the server check, tensors,
and evidence that the candidate ran on a hardware AMD GPU, not llvmpipe. Only a passing receipt in
`/var/lib/orbis/gpu-gate.json` lets Orbis' AI mode use Vulkan; otherwise the CPU stays the backend.

```
sudo orbis-ai-gpu campaign --model /var/lib/orbis/models/<model>.gguf \
     --remote-log udp://<Nobara IP>:47999           # hours; resumable
orbis-ai-gpu recommend
sudo orbis-ai-gpu apply
orbis-ai-gpu status
```

On the Nobara, while the campaign runs:
`python3 <orbis-0.2.0>/tools/inference-harness/orbis_infer_harness.py receive`.

## Tensor and logit capture

* Logits: KL divergence files per context, from `llama-perplexity --kl-divergence-base`.
* Tensors: `llama-eval-callback` dumps on CPU and GPU, compared with relative and absolute
  tolerance 1e-3.

Both are kept under the campaign directory for each variant, so a failing case can be sent to
llama.cpp or Mesa developers with the exact tensor name.

## Memory interplay

AI mode raises the GTT target to 80 % of RAM (`orbis-memd`). The `hostmem` variant moves model
buffers into GTT. If the carve-out is the bottleneck, `orbis-mem set-boot --mode ai` writes the
recommended size (and `gtt.txt` with the GE loader). Every change is reported by
`orbis-mem status` and logged.
