"""Command line tool: ``robofleet <command>``.

  robofleet plan --site site.json --fleet fleet_state.json [--offline] [--image cam.jpg]
  robofleet model-check ["message"]
  robofleet printer status
  robofleet printer list
  robofleet printer ask "print the cube" [--dry-run]
  robofleet printer job --request "40 mm cube" | --stl part.stl | --sliced part.gcode.3mf [--dry-run]

Model settings come from VLM_BASE_URL, VLM_MODEL, VLM_API_KEY / OPENROUTER_API_KEY and VLM_THINKING=off.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from .briefing import build_briefing
from .coordinator import Coordinator, load_json
from .model import ModelClient, ModelError


def _stop(text: str) -> None:
    print(f"\nSTOPPED: {text}")
    sys.exit(1)


def _step(text: str) -> None:
    print(f"\n-> {text}")


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------- fleet

def _print_plan(title: str, plan: dict, rejected: list[str]) -> None:
    print(f"\n=== {title} ===")
    for a in plan["assignments"]:
        print(f"  ASSIGN  {a['task_id']} -> {a['robot_id']}   ({a['reason']})")
    for m in plan["robot_messages"]:
        zone = f" [{m['target_zone']}]" if m.get("target_zone") else ""
        print(f"  TELL    {m['robot_id']}: {m['instruction']}{zone}   ({m['reason']})")
    if not plan["assignments"] and not plan["robot_messages"]:
        print("  (no actions)")
    if plan.get("summary"):
        print(f"  SUMMARY {plan['summary']}")
    for r in rejected:
        print(f"  REJECTED by checker: {r}")


def cmd_plan(args) -> None:
    site, fleet = load_json(args.site), load_json(args.fleet)
    if args.show_briefing:
        print(build_briefing(site, fleet))
    model = None if args.offline else ModelClient.from_env()
    decision = Coordinator(site, model, log_path=args.log).plan(fleet, args.image)
    _print_plan("Rule-based baseline", decision.baseline, [])
    if decision.source == "model":
        _print_plan(f"Model plan ({decision.stats['model']}, {decision.stats['latency_s']} s)",
                    decision.plan, decision.rejected)
        print(f"  tokens: {decision.stats['usage']}")
    elif decision.error:
        print(f"\n!! Model failed, using the rule-based plan: {decision.error}")
    if args.log:
        print(f"\nDecision logged to {args.log}")


def cmd_model_check(args) -> None:
    model = ModelClient.from_env()
    message = " ".join(args.message) or "hi"
    print(f"model:    {model.model}\nserver:   {model.base_url}\nthinking: {model.thinking}\nyou:      {message}")
    try:
        reply, stats = model.chat([{"role": "user", "content": message}], timeout=120)
    except ModelError as e:
        _stop(str(e))
    print(f"qwen:     {reply.strip()}")
    print(f"time:     {stats['latency_s']} s   tokens: {stats['usage']}")


# ---------------------------------------------------------------- printer

def _printer_config(path: Path) -> dict:
    if not path.exists():
        _stop(f"{path} not found. Copy examples/printer.example.json to {path} and fill it in.")
    return load_json(path)


def cmd_printer_status(args) -> None:
    from .printers import BambuPrinter

    printer = BambuPrinter.from_config(_printer_config(args.config)["printer"])
    status = printer.status()
    print(json.dumps({k: status.get(k) for k in
                      ("gcode_state", "mc_percent", "mc_remaining_time", "nozzle_temper",
                       "bed_temper", "subtask_name", "print_error")}, indent=2))


def cmd_printer_list(args) -> None:
    from .printers import list_print_files

    files = list_print_files(args.prints)
    print("\n".join(files) if files else f"No *.gcode.3mf files in {args.prints}")


def _confirm_and_print(printer, path: Path, pcfg: dict, skip_confirm: bool) -> str:
    from .printers import check_printer_ready

    _step("Checking the printer")
    problems = check_printer_ready(printer.status())
    if problems:
        _stop("; ".join(problems))
    print("   printer is idle")
    if not skip_confirm:
        if input(f"\nPrint {path.name} now? Make sure the bed is clear. Type PRINT to start: ").strip() != "PRINT":
            _stop("not confirmed")
    _step("Uploading to printer")
    url = printer.upload(path)
    _step("Starting print")
    printer.start_print(url, use_ams=pcfg.get("use_ams", False))
    print("   sent. Watch progress with: robofleet printer status")
    return url


def cmd_printer_ask(args) -> None:
    from .printers import NO_FILE, BambuPrinter, choose_print_file, list_print_files

    files = list_print_files(args.prints)
    if not files:
        _stop(f"No *.gcode.3mf files in {args.prints}. Export a sliced plate from Bambu Studio into it.")
    request = " ".join(args.request)
    if not request:
        _stop('Say what to print, e.g.  robofleet printer ask "print the cube"')

    _step("Asking the model which file to print")
    try:
        choice, stats = choose_print_file(ModelClient.from_env(), request, files)
    except ModelError as e:
        _stop(str(e))
    print(f"   picked: {choice['file']}  ({choice.get('reason', '')})  [{stats['latency_s']} s]")
    if choice["file"] == NO_FILE:
        _stop("The model did not pick a file.")
    if args.dry_run:
        return

    pcfg = _printer_config(args.config)["printer"]
    url = _confirm_and_print(BambuPrinter.from_config(pcfg), Path(args.prints) / choice["file"], pcfg, args.yes)
    _append_jsonl(Path("logs/print_jobs.jsonl"),
                  {"ts": time.time(), "request": request, "choice": choice, "stats": stats, "started": url})


def cmd_printer_job(args) -> None:
    from .printers import (BambuPrinter, CadError, SlicerError, check_size, design_part, render_stl,
                           slice_stl, stl_size)

    config = _printer_config(args.config)
    pcfg = config["printer"]
    job_dir = Path("jobs") / time.strftime("%Y%m%d-%H%M%S")
    job_dir.mkdir(parents=True, exist_ok=True)
    record: dict = {"ts": time.time(), "dir": str(job_dir)}

    stl, sliced = args.stl, args.sliced
    if args.request:
        record["request"] = args.request
        _step("Asking the model to design the part")
        try:
            design, stats = design_part(ModelClient.from_env(), args.request, pcfg["build_volume_mm"])
        except (ModelError, CadError) as e:
            _stop(str(e))
        name = re.sub(r"[^a-z0-9_]+", "_", design["name"].lower()).strip("_") or "part"
        print(f"   {name}: expected {design['expected_size_mm']} mm ({stats['latency_s']} s)")
        print(f"   notes: {design['notes']}")
        record.update(design=design, stats=stats)
        scad = job_dir / f"{name}.scad"
        scad.write_text(design["openscad"])
        stl = job_dir / f"{name}.stl"
        _step("Rendering the model with OpenSCAD")
        try:
            render_stl(config.get("openscad", "openscad"), scad, stl)
        except (CadError, FileNotFoundError) as e:
            _stop(f"{e}\n   The OpenSCAD code is saved at {scad}")

    if stl:
        _step("Checking part size")
        size = stl_size(stl)
        print(f"   actual size: {size[0]:.1f} x {size[1]:.1f} x {size[2]:.1f} mm")
        problems = check_size(size, pcfg["build_volume_mm"])
        if problems:
            _stop("; ".join(problems))
        record["size_mm"] = size
        sliced = job_dir / f"{Path(stl).stem}.gcode.3mf"
        _step("Slicing with Bambu Studio")
        try:
            slice_stl(config["slicer"], Path(stl), sliced)
        except (SlicerError, FileNotFoundError) as e:
            _stop(str(e))
        print(f"   {sliced}")

    if args.dry_run:
        print(f"\nDry run finished. Files are in {job_dir}")
        return

    record["started"] = _confirm_and_print(BambuPrinter.from_config(pcfg), Path(sliced), pcfg, args.yes)
    _append_jsonl(Path("logs/print_jobs.jsonl"), record)


# ---------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robofleet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="run one fleet coordination cycle")
    p.add_argument("--site", type=Path, required=True, help="site file: zones, chargers, rules")
    p.add_argument("--fleet", type=Path, required=True, help="fleet state: robots, pending tasks, alerts")
    p.add_argument("--image", type=Path, action="append", default=[], help="camera snapshot (repeatable)")
    p.add_argument("--offline", action="store_true", help="skip the model, use rules only")
    p.add_argument("--show-briefing", action="store_true", help="print what the model reads")
    p.add_argument("--log", type=Path, default=Path("logs/decisions.jsonl"), help="JSONL decision log")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("model-check", help="send one message to the model")
    p.add_argument("message", nargs="*")
    p.set_defaults(func=cmd_model_check)

    printer = sub.add_parser("printer", help="Bambu Lab printer commands")
    psub = printer.add_subparsers(dest="printer_command", required=True)

    def printer_cmd(name: str, func, help_text: str) -> argparse.ArgumentParser:
        sp = psub.add_parser(name, help=help_text)
        sp.add_argument("--config", type=Path, default=Path("config/printer.json"))
        sp.add_argument("--prints", type=Path, default=Path("prints"), help="folder of sliced *.gcode.3mf files")
        sp.set_defaults(func=func)
        return sp

    printer_cmd("status", cmd_printer_status, "is the printer reachable and idle?")
    printer_cmd("list", cmd_printer_list, "files the model can choose from")

    sp = printer_cmd("ask", cmd_printer_ask, "model picks a ready-made file and prints it")
    sp.add_argument("request", nargs="*", help="what to print, in plain English")
    sp.add_argument("--dry-run", action="store_true", help="only show the model's choice")
    sp.add_argument("--yes", action="store_true", help="do not ask before printing")

    sp = printer_cmd("job", cmd_printer_job, "design (OpenSCAD) -> slice -> print")
    source = sp.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", help="what to print, in plain English")
    source.add_argument("--stl", type=Path, help="existing STL to slice and print")
    source.add_argument("--sliced", type=Path, help="existing .gcode.3mf to print as-is")
    sp.add_argument("--dry-run", action="store_true", help="do everything except upload and print")
    sp.add_argument("--yes", action="store_true", help="do not ask before printing")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
