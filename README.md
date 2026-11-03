# robofleet

[![CI](https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/robofleet)](https://pypi.org/project/robofleet/)
[![Python](https://img.shields.io/pypi/pyversions/robofleet)](https://pypi.org/project/robofleet/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Python SDK for coordinating a robot fleet (factory, warehouse, hotel) with a vision-language model such as
**Qwen3.8-27B**, plus Bambu Lab 3D printer control.

```
site (zones, chargers, rules) + fleet state (robots, tasks, alerts) [+ camera images]
   -> briefing -> model -> checker -> plan
              \-> rule-based planner (fallback and baseline)
```

- **The model proposes, code verifies.** Every plan goes through a checker that drops made-up IDs, tasks that would
  drain a battery below reserve, and work sent to robots that are charging or in a fault state.
- **Always has an answer.** If the model is offline or returns garbage, the rule-based plan is used.
- **Any model server.** Works with a local llama-server, vLLM or OpenRouter, through the OpenAI-compatible API.
- **No dependencies** for the core. Printer support adds one (`paho-mqtt`).

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Connect a model (Qwen3.8-27B)](#connect-a-model-qwen38-27b)
- [Python API](#python-api)
- [Describe your site](#describe-your-site)
- [3D printer](#3d-printer)
- [Command line reference](#command-line-reference)
- [Development](#development)

## Requirements

| What | Needed for | Minimum |
|---|---|---|
| Python | everything | 3.10+ |
| A model server | model plans and printer `ask`/`job` | see below; `--offline` mode needs none |
| Qwen3.8-27B on a Mac | local model | Apple Silicon, **24 GB** unified memory (3-bit), 32 GB+ recommended, ~20 GB disk |
| Qwen3.8-27B on NVIDIA | local or cloud model | **20–24 GB** VRAM (GGUF), **40 GB+** (vLLM FP8) |
| Bambu Lab printer | printer commands | LAN Only + Developer Mode, microSD card, same network |
| OpenSCAD + Bambu Studio CLI | `printer job --request` only | optional |

Full model sizes and setup are in [docs/MODEL_SETUP.md](docs/MODEL_SETUP.md).

## Install

```sh
pip install robofleet                # core
pip install "robofleet[printer]"     # with Bambu Lab printer support
```

Using a virtual environment is recommended:

```sh
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install "robofleet[printer]"
```

Latest development version from GitHub:

```sh
pip install "robofleet[printer] @ git+https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b.git"
```

## Quick start

No model needed. This runs the rule-based planner on the example warehouse:

```sh
git clone https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b.git && cd vlm_robofleetSDK_qwen3.8_27b
pip install -e .
robofleet plan --site examples/warehouse/site.json --fleet examples/warehouse/fleet_state.json --offline
```

```
=== Rule-based baseline ===
  ASSIGN  T-901 -> RBT-003   (Nearest available robot (21 m).)
  ASSIGN  T-902 -> RBT-001   (Nearest available robot (0 m).)
  ASSIGN  T-903 -> RBT-002   (Nearest available robot (0 m).)
```

Then connect a model (next section) and drop `--offline`.

## Connect a model (Qwen3.8-27B)

robofleet reads four environment variables:

| Variable | Meaning | Default |
|---|---|---|
| `VLM_BASE_URL` | OpenAI-compatible server URL ending in `/v1` | `https://openrouter.ai/api/v1` |
| `VLM_MODEL` | model name | `qwen/qwen3.8-27b` |
| `VLM_API_KEY` or `OPENROUTER_API_KEY` | API key. Not needed for a local server | none |
| `VLM_THINKING` | `off` skips the model's reasoning step, for the fastest answers | on |

**Local on a Mac** (24 GB+ Apple Silicon). Full guide and troubleshooting: [docs/MODEL_SETUP.md](docs/MODEL_SETUP.md).

```sh
brew install llama.cpp
python3 -m pip install -U huggingface_hub
hf download unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-Q3_K_XL.gguf mmproj-F16.gguf --local-dir ~/models/qwen27b
sudo sysctl iogpu.wired_limit_mb=19456        # 24 GB Mac; resets on reboot

llama-server -m ~/models/qwen27b/Qwen3.8-27B-UD-Q3_K_XL.gguf --mmproj ~/models/qwen27b/mmproj-F16.gguf \
  -ngl 99 -c 8192 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 --jinja --reasoning off --port 8000
```

In a second terminal:

```sh
export VLM_BASE_URL=http://localhost:8000/v1 VLM_MODEL=qwen VLM_THINKING=off
robofleet model-check "hi"
```

**NVIDIA GPU with vLLM** (40 GB+):

```sh
vllm serve Qwen/Qwen3.8-27B --quantization fp8 --max-model-len 16384 --port 8000 --api-key <key>
export VLM_BASE_URL=http://<gpu-host>:8000/v1 VLM_MODEL=Qwen/Qwen3.8-27B VLM_API_KEY=<key> VLM_THINKING=off
```

**Hosted** (no hardware needed):

```sh
export OPENROUTER_API_KEY=sk-or-...
```

## Python API

```python
from robofleet import Coordinator, ModelClient, load_json

site = load_json("examples/warehouse/site.json")
fleet = load_json("examples/warehouse/fleet_state.json")

model = ModelClient(base_url="http://localhost:8000/v1", model="qwen", thinking=False)
# or ModelClient.from_env(), or None for rules only

coordinator = Coordinator(site, model, log_path="logs/decisions.jsonl")
decision = coordinator.plan(fleet, images=["camera.jpg"])

decision.source     # "model" or "rules"
decision.plan       # checked plan: {"assignments": [...], "robot_messages": [...], "summary": "..."}
decision.rejected   # what the checker removed from the model's plan
decision.error      # why the model was not used, if it failed
```

| Name | What it does |
|---|---|
| `Coordinator(site, model=None, log_path=None)` | `.plan(fleet, images=())` returns a `Decision` |
| `Decision` | `plan`, `source`, `baseline`, `rejected`, `model_raw`, `stats`, `error`, `.to_record()` |
| `ModelClient(base_url, model, api_key=None, thinking=True, temperature=0.2, timeout=90)` | `.chat()`, `.chat_json()`, `ModelClient.from_env()` |
| `validate_plan(plan, site, fleet)` | the checker, usable on its own; returns `(approved, rejected)` |
| `rule_based_plan(site, fleet)` | the fallback planner |
| `build_messages(site, fleet, images)` | the exact messages the model reads |
| `load_json(path)` | reads a site, fleet or printer file |
| `robofleet.printers` | `BambuPrinter`, `choose_print_file`, `list_print_files`, `check_printer_ready`, `design_part`, `slice_stl` |

Robot instructions the model may send: `go_to_charger`, `pause`, `resume`, `go_to_zone`.

## Describe your site

**Site file**: fixed knowledge. `type` is free text, such as `factory`, `warehouse` or `hotel`, and the model's
instructions use it.

```json
{
  "name": "Demo warehouse",
  "type": "warehouse",
  "zones": {"Zone A3": {"x": 0, "y": 0}, "Loading Zone": {"x": 40, "y": 10}, "Charging Bay": {"x": 0, "y": 15}},
  "chargers": ["Charging Bay"],
  "rules": {"battery_reserve_pct": 20, "charge_threshold_pct": 25, "max_queued_tasks_per_robot": 1}
}
```

**Fleet state**: a live snapshot, built from your robots' telemetry before each `plan()` call.

```json
{
  "robots": [{"id": "RBT-001", "zone": "Zone A3", "battery": 78, "status": "busy", "queued_tasks": []}],
  "pending_tasks": [{"id": "T-901", "type": "transport", "from_zone": "Loading Zone", "to_zone": "Zone A3",
                     "priority": "urgent", "battery_cost": 30}],
  "alerts": [{"source": "RBT-002 camera", "zone": "Zone A3", "text": "Spilled boxes blocking the aisle."}]
}
```

`status` is one of `idle`, `busy`, `charging` or `fault`. Robots that are `charging` or in `fault` never get tasks.
Complete examples are in [`examples/warehouse/`](examples/warehouse/).

## 3D printer

The model picks one of your pre-sliced files and starts it on a Bambu Lab printer over the local network, after
a person types `PRINT`. Setup, including LAN mode, the access code and slicing files, is in
[docs/PRINTER_SETUP.md](docs/PRINTER_SETUP.md).

```sh
pip install "robofleet[printer]"
cp examples/printer.example.json config/printer.json     # fill in ip, serial, access_code
robofleet printer status
robofleet printer ask "print the cube"
```

```python
from robofleet import ModelClient, load_json
from robofleet.printers import BambuPrinter, choose_print_file, list_print_files

printer = BambuPrinter.from_config(load_json("config/printer.json")["printer"])
choice, _ = choose_print_file(ModelClient.from_env(), "print the cube", list_print_files("prints"))
print(choice["file"], printer.status()["gcode_state"])
```

## Command line reference

```
robofleet plan --site FILE --fleet FILE [--image IMG ...] [--offline] [--show-briefing] [--log FILE]
robofleet model-check [MESSAGE]
robofleet printer status                    [--config config/printer.json]
robofleet printer list                      [--prints prints/]
robofleet printer ask REQUEST [--dry-run] [--yes]
robofleet printer job (--request TEXT | --stl FILE | --sliced FILE) [--dry-run] [--yes]
```

`python -m robofleet ...` works too. Add `--help` to any command for details.

## Development

```sh
git clone https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b.git && cd vlm_robofleetSDK_qwen3.8_27b
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[printer]"
python -m unittest discover -s tests -t .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines and [docs/PUBLISHING.md](docs/PUBLISHING.md) for how
releases reach PyPI.

## Security

Never commit `config/printer.json`, API keys or `.env` files. They are git-ignored. See [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
