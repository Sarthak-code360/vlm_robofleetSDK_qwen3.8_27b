# Autonomous factory

The central model (Qwen3.8-27B) is the factory's brain. People ask it for work in plain English. It starts the right
machine, watches the job, and when the job needs something physical done, such as collecting a finished print, it
picks the best robot and sends that robot the task. Each robot's own local AI/controller does the physical work.

```
 user: "print the actuator housing"
        │
        ▼
 ┌──────────────────────────────┐   checks every decision in code
 │  Factory brain (robofleet)   │── model picks machine + file ──► PRINTER-1 (Bambu A1)
 │  central Qwen3.8-27B         │◄─ watches progress ──────────────┘
 │                              │
 │  print done → "collect part" │── model picks robot by distance,
 │  task                        │   battery, workload (checked; rules as fallback)
 └──────────────┬───────────────┘
   heartbeat ▲  │ task / instructions
             │  ▼
   RBT-001 … RBT-010  ──► robot's local VLA / controller does the pick-and-place
```

This follows the multi-robot architecture: robots send their state (identity, location, battery, task status) to
the central model, and the central model sends task instructions back. The parts map as follows:

| Architecture | robofleet |
|---|---|
| Central vision-language model | `robofleet factory serve` with Qwen as `VLM_BASE_URL` |
| Observation tokens + metadata from robots | robot heartbeats: zone, battery, status (+ any extra fields) |
| Task instructions to robots | the task in the heartbeat reply (`instruction`, `from_zone`, `to_zone`) |
| Robot's own VLA model | the command `robofleet robot run --exec` starts per task (e.g. SO-101 `run_fast.sh`) |
| Description file interface | the factory file (`examples/actuator_factory/factory.json`) |
| Developer APIs | the HTTP API below, and the Python SDK |

## Demo: 3D-printed actuator workflow

**What it shows:** a request starts the 3D printer, the factory watches the print, and when it finishes, the nearest
suitable robot out of 10 is sent to take the part from the Print Bay to the Output Box.

### 1. Try it in simulation (no printer, no robots, no model)

Use three terminals, all in the project folder with the virtual environment active:

```sh
# terminal 1: the factory brain, with a simulated printer and rules instead of the model
robofleet factory serve --config examples/actuator_factory/factory.json --simulate-machines --offline

# terminal 2: ten simulated robots
robofleet robot sim --count 10

# terminal 3: ask for work
robofleet factory request "print the actuator housing" --confirm
robofleet factory status
```

Open **http://localhost:8080** to watch the dashboard. You can also type requests there and press **Start** to
confirm jobs.

### 2. With Qwen as the brain

Start the model first (see [MODEL_SETUP.md](MODEL_SETUP.md)), then drop `--offline`:

```sh
export VLM_BASE_URL=http://localhost:8000/v1 VLM_MODEL=qwen VLM_THINKING=off
robofleet factory serve --config examples/actuator_factory/factory.json --simulate-machines
```

Qwen now decides which machine and file match the request and which robot collects the part. Every decision is
checked in code, so a made-up file, a busy machine, or a robot with too little battery is rejected. If the model
fails or is unreachable, keyword matching and the nearest-robot rules take over, so the factory keeps working.

On a 24 GB Mac each decision takes from a few seconds to about a minute, depending on how many robots are online.
Set `"dispatch": "rules"` in the factory file to let only the requests go through the model.

### 3. With the real printer

1. Set up the printer and `config/printer.json` ([PRINTER_SETUP.md](PRINTER_SETUP.md)).
2. Export sliced files with clear names into `prints/`, e.g. `actuator_housing.gcode.3mf`,
   `actuator_gear.gcode.3mf`. The model chooses by file name.
3. Run without `--simulate-machines`:
   ```sh
   robofleet factory serve --config examples/actuator_factory/factory.json
   ```
4. Request, check the bed is clear, then confirm (`robofleet factory confirm JOB-0001` or the dashboard's Start button).

The factory watches the printer every 10 s (`poll_seconds`) and creates the collect task when it reports `FINISH`.

### 4. With a real robot

Run one agent per robot, on the robot's computer or next to it. The agent sends heartbeats and runs a command for
each task. The command gets the task's text, so the robot's own model can carry it out:

```sh
# on the robot's computer, inside the so101-vlm-agent folder
export ROBOFLEET_SERVER=http://<factory-computer-ip>:8080
robofleet robot run --id RBT-001 --name "SO-101 arm" --zone "Assembly Table" \
  --exec 'bash real/run_fast.sh {instruction}'
```

- Placeholders: `{instruction}`, `{from_zone}`, `{to_zone}`, `{task_id}`. Values are shell-quoted for you.
- Exit code 0 means done, anything else means failed. The task's result shows in the dashboard.
- Start the factory with `--host 0.0.0.0` so robots on the network can reach it, and set a token (see Security).
- Simulated and real robots can run together. Any robot ID that sends a heartbeat joins the factory, even if it is
  not listed in the factory file.

## The factory file

```json
{
  "name": "Actuator Factory Demo",
  "type": "3D printed actuator manufacturing factory",
  "zones": {"Print Bay": {"x": 0, "y": 0}, "Output Box": {"x": 12, "y": 0}, "Charging Dock": {"x": 12, "y": 8}},
  "chargers": ["Charging Dock"],
  "default_output_zone": "Output Box",
  "rules": {"battery_reserve_pct": 20, "charge_threshold_pct": 25, "max_queued_tasks_per_robot": 1, "task_battery_cost": 5},
  "require_confirmation": true,
  "dispatch": "model",
  "robot_timeout_s": 15,
  "machines": [
    {"id": "PRINTER-1", "type": "3d_printer", "driver": "bambu", "zone": "Print Bay",
     "config": "../../config/printer.json", "prints_dir": "../../prints", "poll_seconds": 10,
     "print_seconds": 20, "files": ["actuator_housing.gcode.3mf"]}
  ],
  "robots": [{"id": "RBT-001", "name": "SO-101 arm", "home_zone": "Assembly Table"}]
}
```

| Key | Meaning |
|---|---|
| `type` | The factory theme, e.g. rocket factory or welding factory. The model reads it. |
| `zones` | Named places with x/y positions in metres. Robot distances are computed from these. |
| `require_confirmation` | `true`: a person must confirm before a machine starts. Keep this for printers nobody watches. |
| `dispatch` | `model` (Qwen chooses robots, checked in code) or `rules` (nearest suitable robot, fastest). |
| `robot_timeout_s` | A robot with no heartbeat for this long goes `offline`, and its task goes back in the queue. |
| `machines[].driver` | `bambu` (real printer) or `simulated`. `print_seconds` and `files` are only used by `simulated`. |
| `robots` | Known robots. Optional: robots can also join by sending heartbeats. |

To set up a different factory, copy the file, rename the zones and add your machines and robots.

### Adding new kinds of machines

Machines share one small interface (`robofleet/factory/machines.py`): `describe()`, `list_files()`, `start(file)`
and `poll()`. A CNC, welding or soldering station needs a driver class that implements those four methods and an
entry in `DRIVERS`. The brain, checker, dispatch and dashboard work with it unchanged.

## HTTP API

| Method and path | Body | Returns |
|---|---|---|
| `GET /` | | dashboard |
| `GET /api/state` | | factory, zones, machines, robots, jobs, tasks, events |
| `POST /api/requests` | `{"text": "print the actuator housing"}` | `{"ok", "summary", "job" or "task", "choice", "model"}` |
| `POST /api/jobs/<id>/confirm` | `{}` | the job |
| `POST /api/robots/<id>/heartbeat` | `{"zone", "battery", "status": "idle"\|"busy"\|"charging"\|"fault", ...}` | `{"task": {...} or null, "messages": [...]}` |
| `POST /api/tasks/<id>/complete` | `{"robot_id", "ok": true, "note": ""}` | the task |

A task sent to a robot looks like this:

```json
{"id": "TASK-0002", "type": "pick_and_place", "from_zone": "Print Bay", "to_zone": "Output Box",
 "instruction": "Collect the finished part 'actuator_housing.gcode.3mf' from PRINTER-1 at Print Bay and place it in Output Box.",
 "job_id": "JOB-0001", "battery_cost": 5}
```

Each task is delivered once, in the next heartbeat after it is assigned. A robot that stops sending heartbeats loses
the task to another robot after `robot_timeout_s`.

Job states: `awaiting_confirmation` → `starting` → `running` → `done` → `delivered` (or `failed` / `delivery_failed`).
Task states: `pending` → `assigned` → `in_progress` → `done` (or `failed`).

## Python

```python
from robofleet import ModelClient
from robofleet.factory import load_factory, serve

factory = load_factory("examples/actuator_factory/factory.json", ModelClient.from_env(), simulate_machines=True)
result = factory.submit_request("print the actuator gear")
factory.confirm_job(result["job"]["id"])
serve(factory, host="0.0.0.0", port=8080, token="change-me")
```

## Security

- The factory API starts machines and moves robots. Keep it on a private network or VPN.
- When robots connect over the network, start it with a token: `robofleet factory serve ... --host 0.0.0.0 --token <long random string>`.
  Robots and the CLI read the token from `ROBOFLEET_TOKEN`, and the dashboard from `http://host:8080/#token=<token>`.
- Keep `require_confirmation: true` unless someone can see the machines.
- The model's choices are always checked in code before anything happens. Do not bypass the checker.
