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

Open **http://localhost:8080** to watch the dashboard. You can also create work orders there and press
**Review & Start** to approve jobs. Instead of `robofleet robot sim`, you can register digital-twin robots in the
dashboard (Robot Fleet → Register Robot → Batch).

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
4. Request, check the bed is clear, then confirm (`robofleet factory confirm JOB-0001` or **Review & Start** in the dashboard).

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

- Placeholders: `{instruction}`, `{from_zone}`, `{to_zone}`, `{task_id}`, `{document}` (local path of the work
  instruction PDF, or empty). Values are shell-quoted for you.
- Exit code 0 means done, anything else means failed. The task's result shows in the dashboard.
- Start the factory with `--host 0.0.0.0` so robots on the network can reach it, and set a token (see Security).
- Simulated and real robots can run together. Any robot ID that sends a heartbeat joins the factory, even if it is
  not listed in the factory file.

## StarForge AI: the AI worker

The factory has its own AI worker, **StarForge AI** (the name is set by `assistant_name` in the factory file). It
runs on the central model (Qwen3.8-27B), but the dashboard only shows the StarForge name. Use the **StarForge AI**
card on the Overview and type what you need in plain words:

| You type | What happens |
|---|---|
| `print the actuator gear` | A free machine that has that file starts right away. When it finishes, the best robot collects the part. |
| `print the cap on PRINTER-2, RBT-003 delivers it` | Uses that machine and that robot. You can also pick them in the Machine and Robot menus under the message box. |
| `make 10 actuators` | Starts a production run of 10 units (see Production runs). |
| `move the box from Raw Material Store to Assembly Table` | A robot task. |
| `what's the factory status?` · `which robots are available?` | An answer from the live factory data. |

The paperclip attaches a process PDF to the task, and the robot receives it. Progress of everything started from
the chat (print started, finished, robot assigned, picked up, delivered) is posted back as updates.

The AI worker also raises **alerts**: no machine is connected ("add a machine"), no machine has the file, every
suitable machine is busy, no robot can collect a finished part ("add a robot"), or a part has waited longer than
`robot_wait_alert_s` (default 90 s) for a robot.

Every choice the model makes is checked in code: the machine must exist, have the file, be idle, and not still
hold its previous part. If the model is offline, simple keyword matching keeps the chat working ("limited mode").

API: `POST /api/chat` `{"text", "machine_id"?, "robot_id"?, "document_id"?}`. The conversation is in `/api/state`
under `chat`.

## Operations dashboard

`robofleet factory serve` serves the dashboard at `http://<host>:8080/`.

| Tab | What you can do |
|---|---|
| Production | The page that opens first. **Set up once, then Run Factory**: (1) the workflow/process PDF, (2) the robots and (3) machines that are registered, (4) product name, target quantity, parts per unit and delivery zone. The right side tracks the run: progress, units produced against the target, throughput, time remaining, per-part counts and live activity. Pause, resume or stop at any time. |
| Overview | Robots ready, machines working, robot battery; the **StarForge AI** chat (type what you need, pick a machine or robot, attach a PDF); the Live card (parts delivered today, each machine's state, robots on the move); the factory floor. |
| Settings | Robots, Machines, Zones and Documents. Add robots (one or many, simulated or real) and machines (simulated or Bambu Lab) with a name and a location; technical fields are under "More options". **Click a machine** to see its print files, **upload a new file from your computer** (a sliced `.gcode.3mf`), press **Print** on any file, or delete uploaded files. |
| Activity | Updates (everything that happened, filterable) and Jobs (machine jobs and robot tasks). |

**Production runs.** A run makes `target` units of a product, each needing a set of parts (job files and how
many of each). While the run is active, the factory keeps every free machine busy with the part that is most
needed. A machine counts as free only after the robot has collected its previous part, so nothing piles up on a
print bed. Each finished part becomes a robot task with the process PDF attached, and a unit counts as produced
once all its parts are delivered. The run completes at the target. Three failed jobs in a row pause it.
If you tick no parts, the model picks them from the product name; without a model, file-name matching is used.
With real machines, "Operator approves every machine start" is on by default, so a person confirms the bed is
clear before each print.

```sh
curl -X POST localhost:8080/api/runs -d '{"name": "Actuator Assembly", "target": 12,
  "parts": {"actuator_housing.gcode.3mf": 1, "actuator_gear.gcode.3mf": 1, "actuator_cap.gcode.3mf": 1},
  "document_id": "DOC-001", "approve_each_job": false}'
```

**Robot connections.** A *digital twin* robot is simulated inside the server, which is good for demos and for
planning. An *edge agent* is a real robot. Run the agent on or next to it; the dashboard shows the exact command:

```sh
robofleet robot run --server http://<factory-ip>:8080 --id RBT-011 --exec 'bash real/run_fast.sh {instruction}'
```

**Work instruction PDFs.** A PDF attached to a work order travels with the robot task. The agent downloads it to
`~/.robofleet/documents/` before running the task, and `{document}` in the `--exec` command is its local path, for
example `--exec 'python my_robot.py --task {instruction} --sop {document}'`. The model is told the document's name.

**Saved data.** Robots, machines and zones added in the dashboard, and uploaded documents, are kept in `--data-dir`
(default `data/`; it is git-ignored). Printer access codes are stored there (file mode 600) and never sent back to
the browser. Robots and machines from the factory file are shown as *factory file* and can only be removed there.

## The factory file

```json
{
  "name": "Actuator Factory Demo",
  "type": "3D printed actuator manufacturing factory",
  "zones": {"Print Bay": {"x": 0, "y": 0}, "Output Box": {"x": 12, "y": 0}, "Charging Dock": {"x": 12, "y": 8}},
  "chargers": ["Charging Dock"],
  "default_output_zone": "Output Box",
  "rules": {"battery_reserve_pct": 20, "charge_threshold_pct": 25, "max_queued_tasks_per_robot": 1, "task_battery_cost": 5},
  "assistant_name": "StarForge AI",
  "require_confirmation": false,
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
| `assistant_name` | The AI worker's name in the dashboard and chat (default "StarForge AI"). |
| `require_confirmation` | `false` (the example): machines start on their own, fully autonomous. `true`: a person approves every machine start (Review & Start). |
| `robot_wait_alert_s` | Alert when a finished part has waited this long for a robot (default 90). |
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
| `GET /api/state` | | factory, zones, machines, robots, jobs, tasks, documents, machine history, events |
| `POST /api/requests` | `{"text", "document_id"?, "deliver_to"?, "priority"?: "normal"\|"urgent"}` | `{"ok", "summary", "job" or "task", "choice", "model"}` |
| `POST /api/jobs/<id>/confirm` | `{}` | the job |
| `POST /api/jobs/<id>/cancel` | `{}` | the job (only while awaiting approval) |
| `POST /api/robots` | `{"id"?, "name", "kind", "model", "home_zone", "payload_kg", "capabilities", "connection": "simulated"\|"agent", "battery", "count"}` | `{"robots": [...]}` |
| `DELETE /api/robots/<id>` | | the robot |
| `POST /api/robots/<id>/heartbeat` | `{"zone", "battery", "status": "idle"\|"busy"\|"charging"\|"fault", ...}` | `{"task": {...} or null, "messages": [...]}` |
| `POST /api/tasks/<id>/complete` | `{"robot_id", "ok": true, "note": ""}` | the task |
| `GET /api/machines/<id>/files` | | the machine's print files (name, size, date, deletable) |
| `POST /api/machines/<id>/files` | raw `.gcode.3mf` body, header `X-Filename` (max 500 MB) | the stored file |
| `DELETE /api/machines/<id>/files/<name>` | | removes an uploaded file |
| `POST /api/machines/<id>/print` | `{"file", "robot_id"?}` | the job: prints now, a robot collects the part |
| `POST /api/machines` | `{"id", "name", "type", "driver": "simulated"\|"bambu", "zone", ...}` (simulated: `files`, `print_seconds`; bambu: `ip`, `serial`, `access_code`, `use_ams`, `prints_dir`) | the machine (without the access code) |
| `DELETE /api/machines/<id>` | | the machine |
| `POST /api/zones` | `{"name", "x", "y"}` | the zone |
| `POST /api/runs` | `{"name", "target", "parts"?: {file: qty}, "description"?, "document_id"?, "deliver_to"?, "approve_each_job"?}` | the run with live counts |
| `POST /api/runs/<id>/pause` · `resume` · `stop` | `{}` | the run |
| `POST /api/documents` | raw PDF body, header `X-Filename` | `{"id", "name", "size", "uploaded"}` |
| `GET /api/documents/<id>` | | the PDF (`?token=` works too) |
| `DELETE /api/documents/<id>` | | the document |

A task sent to a robot looks like this:

```json
{"id": "TASK-0002", "type": "pick_and_place", "from_zone": "Print Bay", "to_zone": "Output Box",
 "instruction": "Collect the finished part 'actuator_housing.gcode.3mf' from PRINTER-1 at Print Bay and place it in Output Box. Follow the work instruction 'Housing SOP.pdf'.",
 "job_id": "JOB-0001", "priority": "normal", "battery_cost": 5,
 "document": {"id": "DOC-001", "name": "Housing SOP.pdf", "url": "/api/documents/DOC-001"}}
```

Each task is delivered once, in the next heartbeat after it is assigned. A robot that stops sending heartbeats loses
the task to another robot after `robot_timeout_s`.

Job states: `awaiting_confirmation` → `starting` → `running` → `done` → `delivered` (or `failed` / `delivery_failed` / `cancelled`).
Task states: `pending` → `assigned` → `in_progress` → `done` (or `failed`).

## Python

```python
from robofleet import ModelClient
from robofleet.factory import load_factory, serve

factory = load_factory("examples/actuator_factory/factory.json", ModelClient.from_env(), simulate_machines=True,
                       data_dir="data")
factory.add_robots({"name": "Mobile Manipulator", "home_zone": "Robot Park", "connection": "simulated", "count": 5})
doc = factory.add_document("Gear SOP.pdf", open("gear_sop.pdf", "rb").read())
result = factory.submit_request("print the actuator gear", document_id=doc["id"], priority="urgent")
factory.confirm_job(result["job"]["id"])
serve(factory, host="0.0.0.0", port=8080, token="change-me")
```

## Security

- The factory API starts machines and moves robots. Keep it on a private network or VPN.
- When robots connect over the network, start it with a token: `robofleet factory serve ... --host 0.0.0.0 --token <long random string>`.
  Robots and the CLI read the token from `ROBOFLEET_TOKEN`, and the dashboard from `http://host:8080/#token=<token>`.
- The example runs fully autonomously (`require_confirmation: false`). A machine is never started while it still
  holds its previous part, and the Bambu driver checks that the printer is idle and error-free before each print.
  Set `require_confirmation: true` if a person must check the bed before every print.
- The model's choices are always checked in code before anything happens. Do not bypass the checker.
