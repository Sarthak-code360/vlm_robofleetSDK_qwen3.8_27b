# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
