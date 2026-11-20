# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Live printer camera (Bambu Lab A1 / A1 mini / P1, LAN mode, port 6000): `robofleet.printers.camera.BambuCamera`
  reads the JPEG stream only while someone is watching. `GET /api/machines/<id>/camera.jpg` returns the newest
  picture. The Overview shows a camera panel with the printer's state, job, progress, time left and temperatures,
  and the camera is also in Settings → Machines → the printer. Set `"camera": false` on a machine to turn it off.

### Fixed

- Production progress now moves while a part is being made. It used to stay at 0% and jump to 100% when the part
  was delivered. A part counts its machine's progress (up to 90%), 95% while waiting for a robot, and 100% when
  delivered. The run panel shows a "Now" column ("Printing 38% · 1 min left"), and the time remaining uses the
  printer's own estimate when everything left is already on a machine. Machines report this through
  `job_progress()`, without extra network calls.

## [0.9.0] - 2026-11-19

### Added

- Machine catalog (`robofleet/factory/catalog.py`): 30 stationary machine types (3D and resin printers, CNC
  mill, router and lathe, lathe, drill, grinder, wire EDM, saw, CO2 and fiber lasers, plasma, waterjet, shear,
  press brake, presses, injection moulding, vacuum former, composite cell, spot welder, welding robot, pick-and-place,
  reflow oven, assembly, heat treatment, paint, inspection, packaging). Each has its inputs and stage. 3D printers are
  live; the rest are simulated.
- Production step 4: choose machines (several) and fill in each machine's inputs. A 3D printer asks for the
  `.gcode.3mf` (choose or upload). Runs accept `steps`. Missing machine types are added as simulated machines
  (`POST /api/machines/ensure`).
- Simulated machines accept uploaded job files of their type (DXF, G-code, ...), kept in `<data-dir>/jobs/`.
- StarForge AI printing: "print a duck" reports every printer's state and asks for the model file. A `.gcode.3mf`
  attached with 📎 is stored on the printer and printed at once if it is connected and free (`print_file` in
  `/api/chat`).
- `assistant_instructions` in the factory file personalise StarForge AI. Machines in its briefing now include their
  type and whether they are live.

### Fixed

- `.gitignore` ignored `src/robofleet/factory/static/three/build/`, so the 3D viewer was missing from the repository.

## [0.8.0] - 2026-11-18

### Added

- 3D showcase on the Overview: the factory file's `showcase` model (a `.glb`) is shown in place of the Live card,
  rotating, with drag to orbit and scroll to zoom, plus pause and reset buttons. While production runs, it shows
  "N of M built" and a progress bar. The rocket factory shows the full rocket assembly (`model/rocket_assembly.glb`).
  three.js is bundled, so it works without internet. API: `GET /api/showcase`, `GET /static/...`.

### Changed

- Faster StarForge replies, and no more "Is there anything else...?" closing lines (see 0.7.0 notes below).
- `.gitignore` excludes large CAD exports in `model/` (`*.obj`, `*.mtl`, `*.stl`).

## [0.7.0] - 2026-11-17

### Added

- Rocket manufacturing demo (`examples/rocket_factory/factory.json`) replaces the actuator demo. 3D printer (Bambu
  Lab A1), CNC mill, robotic welder and composite layup cell make the parts. Robots bring them to final assembly,
  then paint, quality inspection and shipping. 11 zones, 10 robots.
- Machine types beyond 3D printers: `cnc_machine`, `welding_station`, `composite_cell`, `assembly_station`,
  `paint_booth`, `inspection_station`, run by the new `SimulatedStation` driver. Add them from Settings → Machines.
- Build order in production runs: a later-stage job (assembly → paint → inspection) starts only once the unit's
  earlier-stage parts are delivered, and each finished part is routed to the next station's zone.
- Product catalog (`products` in the factory file): "Sounding Rocket" lists everything one rocket needs. Production
  setup fills in the parts when you pick a product, and "make 2 rockets" in the chat finds it.

### Changed

- The Overview chat box keeps a fixed height and scrolls.
- Faster StarForge replies: a smaller factory summary (about 40% fewer tokens), only the needed JSON fields are
  generated, replies are capped at 400 tokens, and the example uses rule-based robot dispatch, so the model is
  free for the chat.
- StarForge no longer ends answers with "Is there anything else...?" (instructed, and removed in code).
- `--simulate-machines` now simulates every machine, not only printers.

## [0.6.0] - 2026-11-17

### Added

- Print files per machine: in Settings → Machines, click a machine to see its files, upload a sliced `.gcode.3mf`
  from your computer (streamed, up to 500 MB, checked to be a real 3MF), print any file with one click, or delete
  uploaded files. API: `GET|POST /api/machines/<id>/files`, `DELETE /api/machines/<id>/files/<name>`,
  `POST /api/machines/<id>/print`.
- `robofleet factory serve --simulate-robots`: brings every robot in the factory file online as a simulated robot.

### Changed

- Light theme.
- Tabs: Production (opens first), Overview, Settings, Activity. The StarForge AI chat now lives on the Overview.
- Fixed: a sent chat message was shown twice until the reply arrived.
- Simulated printers list both their configured files and the files in their prints folder.

## [0.5.0] - 2026-11-17

### Added

- StarForge AI, the factory's AI worker chat (`robofleet/factory/assistant.py`, `POST /api/chat`). Plain-language
  requests start machines, production runs and robot tasks, and questions are answered from live factory data. You
  can choose the machine and robot, or let it pick, and attach a process PDF. Progress updates and alerts (no
  machine, no robot, part waiting too long) are posted to the chat.
- Dashboard: StarForge AI tab with a chat layout (welcome screen, suggestions, composer with machine and robot
  menus and PDF attachment), and an AI Worker panel on the Overview above Machine State.
- Operator-chosen robots: a task can name its robot, and the dispatcher waits for that robot.
- Factory file keys `assistant_name` and `robot_wait_alert_s`.

### Changed

- Simpler dashboard for non-technical users. Tabs: Overview, Production, StarForge AI, Setup (robots, machines,
  zones, documents) and Activity (updates, jobs). The Overview shows only robots ready, machines working, battery,
  the chat, what is happening now, and the factory floor. Labels and messages are in plain words.
- Removed from the Overview: delivery success gauge, machine state timeline, fleet donut, task queue and deliveries
  chart. The Work Orders tab is replaced by the chat.
- Add robot and Add machine ask only for a name, location and type; technical fields are under "More options".
- Chat progress updates are in plain language ("Printer 2 started making the actuator gear.").
- Robots that are offline no longer get charging instructions.
- The example factory is fully autonomous (`require_confirmation: false`).
- A machine that still holds a finished part (waiting for a robot) is not given a new job.

## [0.4.0] - 2026-11-17

### Added

- Production runs: make N units of a product autonomously. Set the parts per unit (or let the model choose them),
  the process PDF and the target. Free machines are kept busy, robots deliver every part, and the run completes at
  the target. Three failed jobs in a row pause it. API: `POST /api/runs`, `POST /api/runs/<id>/pause|resume|stop`.
- Dashboard **Production** tab (the new landing page): setup with process PDF, robots, machines, product and target,
  plus Run Factory; live progress, units against target, throughput, time remaining, per-part counts, activity feed.
  The header shows Factory Running, Paused or Idle.

### Changed

- The dashboard is renamed "Autonomous Factory", with a purple accent.

## [0.3.0] - 2026-11-17

### Added

- Operations Center dashboard: KPI gauges (fleet availability, machine utilization, delivery success, fleet energy),
  machine state timeline, fleet status, deliveries chart, live floor map, approvals banner, activity log with filters.
- Register robots (single or batch), machines (simulated or Bambu Lab LAN) and zones from the dashboard. They are saved
  in `--data-dir` (default `data/`) and reloaded on restart. New API: `POST /api/robots`, `DELETE /api/robots/<id>`,
  `POST /api/machines`, `DELETE /api/machines/<id>`, `POST /api/zones`.
- Digital-twin robots: simulated robots that run inside the server, so no `robofleet robot sim` is needed.
- Work instruction PDFs: upload (`POST /api/documents`), attach to work orders, delivered with the robot task. The
  robot agent downloads them, and `{document}` in `--exec` is the local path.
- Work orders take `deliver_to` and `priority` (`normal` or `urgent`). Jobs awaiting approval can be cancelled
  (`POST /api/jobs/<id>/cancel`).
- `robofleet factory serve --data-dir`.

### Changed

- The dashboard is a separate file (`robofleet/factory/dashboard.html`), shipped in the package.
- Approving a job in the dashboard needs a safety check.
- Example factory renamed to "Actuator Manufacturing Cell", with robot types and payloads.

## [0.2.0] - 2026-11-16

### Added

- `robofleet.factory`: autonomous factory brain. A request goes to the model, which picks the machine and file.
  The job is watched, the follow-up robot task is created, and the model picks the robot by distance, battery and
  workload. Every model decision is checked in code, with keyword matching and rule-based dispatch as fallbacks.
- Factory HTTP API with a live dashboard, robot heartbeats, task delivery and an optional API token.
- `RobotAgent` with `SimulatedExecutor` and `CommandExecutor`. The command executor runs a robot's own local AI per
  task, e.g. SO-101 `run_fast.sh {instruction}`.
- Machine drivers: real Bambu printer (`bambu`) and `simulated`.
- CLI: `robofleet factory serve|request|confirm|status`, `robofleet robot run|sim`.
- Example `examples/actuator_factory/factory.json`: 3D-printed actuator workflow with 10 robots.
- Robots with status `offline` never get tasks. Offline robots' tasks go back in the queue.

### Changed

- The model briefing uses compact JSON, so fewer tokens and faster answers.

### Removed

- `printer job` command and the OpenSCAD design and Bambu Studio CLI slicing pipeline (`design_part`, `render_stl`,
  `slice_stl`, `stl_size`, `check_size`).

## [0.1.0] - 2026-11-03

### Added

- `Coordinator`, `Decision`: one coordination cycle with model plan, checker and rule-based fallback.
- `ModelClient`: OpenAI-compatible client for llama-server, vLLM and OpenRouter, with `thinking=False` support.
- `validate_plan`, `rule_based_plan`, `build_messages`: usable on their own.
- Site `type` (factory, warehouse, hotel, ...) used in the model's instructions.
- `robofleet.printers`: Bambu Lab LAN control (status, upload, start), model-chosen print files, OpenSCAD design and
  Bambu Studio CLI slicing.
- `robofleet` command line tool: `plan`, `model-check`, `printer status|list|ask|job`.
