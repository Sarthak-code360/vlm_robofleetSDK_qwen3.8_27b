# Model setup: Qwen3.8-27B

robofleet talks to any OpenAI-compatible chat server. The default model is **Qwen3.8-27B**, a 27B dense
vision-language model. Pick one of the three options below.

| Option | Good for | Cost |
|---|---|---|
| [A. Local on a Mac (llama.cpp)](#a-local-on-a-mac-llamacpp) | development, demos, offline use | free, slower |
| [B. Local or cloud NVIDIA GPU (vLLM or llama.cpp)](#b-nvidia-gpu-vllm-or-llamacpp) | production, many robots | GPU rental or hardware |
| [C. Hosted API (OpenRouter)](#c-hosted-api-openrouter) | trying it out in 2 minutes | pay per token |

Whichever you choose, turn the model's **thinking/reasoning off** for fast answers: set `VLM_THINKING=off`
(or pass `ModelClient(..., thinking=False)`), and start llama-server with `--reasoning off`.

## Hardware requirements

The model weights must fit in GPU memory (unified memory on Apple Silicon), with a few GB more for context.

| Build | Weights on disk | Apple Silicon Mac | NVIDIA GPU | Quality |
|---|---|---|---|---|
| `UD-Q3_K_XL` (3-bit GGUF) | ~14.4 GB | 24 GB unified memory, minimum | 20 GB+ VRAM | good; tested with robofleet |
| `Q4_K_M` (4-bit GGUF) | ~18 GB | 32 GB unified memory | 24 GB VRAM (RTX 3090/4090) | better |
| FP8 (vLLM) | ~28 GB | not supported | 40 GB+ VRAM (L40S, A100, H100) | near full |
| BF16 (vLLM) | ~54 GB | not supported | 80 GB VRAM (A100/H100 80 GB) | full |

Other requirements:

- **Disk:** 20–35 GB free for the model files.
- **Apple Silicon only.** Intel Macs are too slow.
- **Speed** depends mostly on memory bandwidth. Measured on a 24 GB MacBook Air with the 3-bit build: robofleet's
  short printer prompts answer in about 3–5 s. Long prompts with camera images take much longer, because reading
  the prompt (prefill) runs at about 25–30 tokens/s on a base M-series chip. A data-centre GPU answers the same
  requests in well under a second.

Check your Mac:

```sh
system_profiler SPHardwareDataType | grep -E "Chip|Memory"   # chip and RAM
uname -m                                                     # must be arm64
df -h /                                                      # free disk
```

## A. Local on a Mac (llama.cpp)

1. **Install Homebrew** if `brew --version` fails:
   ```sh
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
   eval "$(/opt/homebrew/bin/brew shellenv)"
   ```
2. **Install llama.cpp and the Hugging Face downloader:**
   ```sh
   brew install llama.cpp python@3.12
   python3.12 -m pip install -U huggingface_hub     # gives the `hf` command
   ```
3. **Download the model and its vision projector** (~15 GB). Use `Q4_K_M` instead on a 32 GB+ Mac:
   ```sh
   mkdir -p ~/models/qwen27b
   hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-Q3_K_XL.gguf mmproj-F16.gguf --local-dir ~/models/qwen27b
   ```
4. **Let the GPU use more memory.** This is needed on 24 GB Macs, and resets on reboot:
   ```sh
   sudo sysctl iogpu.wired_limit_mb=19456      # 24 GB Mac: ~19 GB for the GPU; 32 GB Mac: 26000
   ```
5. **Start the server** and keep this terminal open:
   ```sh
   llama-server \
     -m ~/models/qwen27b/Qwen3.8-27B-UD-Q3_K_XL.gguf \
     --mmproj ~/models/qwen27b/mmproj-F16.gguf \
     -ngl 99 -c 8192 --flash-attn on \
     --cache-type-k q8_0 --cache-type-v q8_0 \
     --jinja --reasoning off \
     --port 8000
   ```
   Wait for `server is listening on http://127.0.0.1:8000`. To run it in the background instead, put
   `nohup ... > ~/llama.log 2>&1 &` around the same command, and stop it with `pkill llama-server`.
6. **Point robofleet at it** in a second terminal:
   ```sh
   export VLM_BASE_URL=http://localhost:8000/v1 VLM_MODEL=qwen VLM_THINKING=off
   robofleet model-check "hi"
   ```

## B. NVIDIA GPU (vLLM or llama.cpp)

**vLLM** (Linux, 40 GB+ GPU, best throughput):

```sh
pip install vllm
vllm serve Qwen/Qwen3.8-27B --quantization fp8 --max-model-len 16384 \
  --limit-mm-per-prompt '{"image":4}' --host 0.0.0.0 --port 8000 --api-key "$(openssl rand -hex 16)"
```

```sh
export VLM_BASE_URL=http://<gpu-host>:8000/v1 VLM_MODEL=Qwen/Qwen3.8-27B VLM_API_KEY=<the key above> VLM_THINKING=off
```

**llama.cpp with CUDA** (20–24 GB GPU, GGUF builds from option A):

```sh
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_CUDA=ON && cmake --build build --config Release -j
./build/bin/llama-server -m ~/models/qwen27b/Qwen3.8-27B-UD-Q3_K_XL.gguf --mmproj ~/models/qwen27b/mmproj-F16.gguf \
  -ngl 99 -c 8192 --flash-attn on --jinja --reasoning off --port 8000
```

If the server is reachable from other machines, always set an API key. Never expose it to the internet without one.

## C. Hosted API (OpenRouter)

```sh
export OPENROUTER_API_KEY=sk-or-...        # from openrouter.ai/keys
robofleet model-check "hi"                 # uses qwen/qwen3.8-27b by default
```

Never commit API keys. Keep them in environment variables or a git-ignored `.env` file.

## Troubleshooting

| Problem | Fix |
|---|---|
| `couldn't bind HTTP server socket ... port 8000` | A server is already running. Check with `curl localhost:8000/health`, or stop it with `pkill llama-server`, or use `--port 8001` |
| `Setting 'enable_thinking' via --chat-template-kwargs is deprecated` | Use `--reasoning off` instead |
| Out of memory / very slow / Memory Pressure red | Use the 3-bit build, lower `-c` (context), close other apps, raise `iogpu.wired_limit_mb` |
| `brew: command not found` in new terminals | Fix `~/.zprofile` to contain `eval "$(/opt/homebrew/bin/brew shellenv)"` |
| Answers start with long `<think>` text | Thinking is still on: use `--reasoning off` and `VLM_THINKING=off` |
