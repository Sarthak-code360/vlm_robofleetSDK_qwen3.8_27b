# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-03

### Added

- `Coordinator`, `Decision`: one coordination cycle with model plan, checker and rule-based fallback.
- `ModelClient`: OpenAI-compatible client for llama-server, vLLM and OpenRouter, with `thinking=False` support.
- `validate_plan`, `rule_based_plan`, `build_messages`: usable on their own.
- Site `type` (factory, warehouse, hotel, ...) used in the model's instructions.
- `robofleet.printers`: Bambu Lab LAN control (status, upload, start), model-chosen print files, OpenSCAD design and
  Bambu Studio CLI slicing.
- `robofleet` command line tool: `plan`, `model-check`, `printer status|list|ask|job`.
