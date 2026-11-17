# robofleet

[![CI](https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b/actions/workflows/ci.yml/badge.svg)](https://github.com/Sarthak-code360/vlm_robofleetSDK_qwen3.8_27b/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/robofleet)](https://pypi.org/project/robofleet/)
[![Python](https://img.shields.io/pypi/pyversions/robofleet)](https://pypi.org/project/robofleet/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Python SDK for autonomous factories and robot fleets, with a vision-language model such as **Qwen3.8-27B** as the
central brain. People ask for work in plain English. The model starts the right machine, such as a 3D printer,
watches the job, and sends the best robot to handle the physical follow-up, such as collecting the finished part.

```
"print the actuator housing"
   -> model picks machine + file -> printer runs -> print done
   -> model picks the robot (distance, battery, workload) -> robot's own AI collects the part
   (every model decision is checked in code; rules take over if the model fails)
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
- [Autonomous factory](#autonomous-factory)
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
| A model server | model decisions | see below; `--offline` mode needs none |
| Qwen3.8-27B on a Mac | local model | Apple Silicon, **24 GB** unified memory (3-bit), 32 GB+ recommended, ~20 GB disk |
| Qwen3.8-27B on NVIDIA | local or cloud model | **20–24 GB** VRAM (GGUF), **40 GB+** (vLLM FP8) |
| Bambu Lab printer | printer commands | LAN Only + Developer Mode, microSD card, same network |

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

Then connect a model (see [Connect a model](#connect-a-model-qwen38-27b)) and drop `--offline`.

## Autonomous factory

Run the 3D-printed actuator demo in simulation, with 10 robots and a simulated printer, in three terminals:

```sh
robofleet factory serve --config examples/actuator_factory/factory.json --simulate-machines --offline
robofleet robot sim --count 10
robofleet factory request "print the actuator housing" --confirm
```

Open **http://localhost:8080** for the **Autonomous Factory** dashboard. The printer "prints" for 20 s. The nearest
suitable robot then picks up the part from the Print Bay and brings it to the Output Box:

```
JOB-0001 started: PRINTER-1 running actuator_housing.gcode.3mf
JOB-0001 finished on PRINTER-1; created TASK-0002 to collect it
TASK-0002 -> RBT-004 (rules: Nearest available robot (6 m).)
RBT-004 picked up TASK-0002: Collect the finished part 'actuator_housing.gcode.3mf' from PRINTER-1 at Print Bay ...
RBT-004 finished TASK-0002
```

The dashboard runs the whole factory without the command line:

- **StarForge AI:** the factory's own AI worker, a chat. Type "print the actuator gear" and a machine starts; when it
  finishes, a robot is assigned and delivers the part. Optionally pick the machine and robot, or attach a process
  PDF. It answers status questions and alerts you when a machine or robot is missing. It also sits on the Overview
  as the AI Worker panel.
- **Production:** set up once (process PDF, robots, machines, product, target quantity, parts per unit) and press
  **Run Factory**. Machines are kept busy, robots deliver every part, and progress is tracked until the target.
- **Overview:** fleet availability, machine utilization, delivery success and fleet energy gauges, a machine state
  timeline, fleet status, deliveries chart and a live floor map with robot positions and active routes.
- **Work Orders:** describe the work in plain language, choose the delivery zone and priority, and attach a work
  instruction PDF for the robot. Jobs wait for **Review & Start** (with a safety check) before a machine starts.
- **Robot Fleet:** register one robot or a batch of robots: type, model, home zone, payload, capabilities. A robot is
  either a *digital twin* (simulated inside the server) or an *edge agent* (a real robot running `robofleet robot run`;
  the dashboard shows the command).
- **Machines & Layout:** register simulated or Bambu Lab printers (IP, serial, access code) and add zones.
- **Documents** and **Activity Log**.

Robots, machines, zones and documents added in the dashboard are saved in `data/` and come back after a restart.

Next steps:

- **Qwen as the brain:** set `VLM_BASE_URL`, `VLM_MODEL` and `VLM_THINKING=off`, then drop `--offline`.
- **The real printer:** drop `--simulate-machines`.
- **A real robot:** run `robofleet robot run --id RBT-001 --exec 'bash real/run_fast.sh {instruction}'`.

Each robot runs its own agent, which sends heartbeats (zone, battery, status) and receives tasks. Machines and
robots are described in one factory file, so a rocket, welding or assembly factory is a different file with
different zones, machines and robots.

Full guide, HTTP API and how to add machines: **[docs/FACTORY.md](docs/FACTORY.md)**.

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
| `robofleet.factory` | `load_factory`, `Factory`, `serve`, `RobotAgent`, `FactoryClient`, machine drivers ([docs](docs/FACTORY.md)) |
| `robofleet.printers` | `BambuPrinter`, `choose_print_file`, `list_print_files`, `check_printer_ready` |

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
robofleet factory serve --config FILE [--simulate-machines] [--offline] [--host H] [--port P] [--token T]
robofleet factory request TEXT [--confirm]
robofleet factory confirm JOB_ID
robofleet factory status
robofleet robot run --id ID [--zone Z] [--exec CMD] [--name N]
robofleet robot sim [--count 10] [--speed 1.0]
robofleet printer status | list | ask REQUEST [--dry-run] [--yes]
robofleet plan --site FILE --fleet FILE [--image IMG ...] [--offline] [--show-briefing]
robofleet model-check [MESSAGE]
```

`factory request/confirm/status` and `robot` commands reach the factory at `ROBOFLEET_SERVER`
(default `http://127.0.0.1:8080`), using `ROBOFLEET_TOKEN` if set. `python -m robofleet ...` works too. Add `--help`
to any command for details.

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
