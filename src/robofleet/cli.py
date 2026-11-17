"""Command line tool: ``robofleet <command>``.

Autonomous factory:
  robofleet factory serve --config examples/actuator_factory/factory.json [--simulate-machines] [--simulate-robots]
  robofleet robot sim --count 10                 # 10 simulated robots
  robofleet robot run --id RBT-001 --exec 'bash real/run_fast.sh {instruction}'   # a real robot
  (robots, machines, zones and work instruction PDFs can also be added in the dashboard)
  robofleet factory request "print the actuator housing"
  robofleet factory confirm JOB-0001
  robofleet factory status

Single printer:
  robofleet printer status | list | ask "print the cube" [--dry-run]

Fleet planning only:
  robofleet plan --site site.json --fleet fleet_state.json [--offline]
  robofleet model-check ["message"]

Model settings: VLM_BASE_URL, VLM_MODEL, VLM_API_KEY / OPENROUTER_API_KEY, VLM_THINKING=off.
Factory settings: ROBOFLEET_SERVER (default http://127.0.0.1:8080), ROBOFLEET_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from pathlib import Path

from .briefing import build_briefing
from .coordinator import Coordinator, load_json
from .model import ModelClient, ModelError

DEFAULT_SERVER = "http://127.0.0.1:8080"


def _stop(text: str) -> None:
    print(f"\nSTOPPED: {text}")
    sys.exit(1)


def _step(text: str) -> None:
    print(f"\n-> {text}")


# ---------------------------------------------------------------- fleet planning

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


# ---------------------------------------------------------------- single printer

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


def cmd_printer_ask(args) -> None:
    from .printers import NO_FILE, BambuPrinter, check_printer_ready, choose_print_file, list_print_files

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
    printer = BambuPrinter.from_config(pcfg)
    _step("Checking the printer")
    problems = check_printer_ready(printer.status())
    if problems:
        _stop("; ".join(problems))
    print("   printer is idle")
    if not args.yes and input(f"\nPrint {choice['file']} now? Make sure the bed is clear. Type PRINT to start: ").strip() != "PRINT":
        _stop("not confirmed")
    _step("Uploading to printer")
    url = printer.upload(Path(args.prints) / choice["file"])
    _step("Starting print")
    printer.start_print(url, use_ams=pcfg.get("use_ams", False))
    print("   sent. Watch progress with: robofleet printer status")


# ---------------------------------------------------------------- factory

def _client(args):
    from .factory import FactoryClient

    return FactoryClient(args.server, args.token)


def cmd_factory_serve(args) -> None:
    from .factory import load_factory, serve

    model = None if args.offline else ModelClient.from_env()
    try:
        factory = load_factory(args.config, model, simulate_machines=args.simulate_machines, log_path=args.log,
                               data_dir=args.data_dir)
    except (ValueError, FileNotFoundError, KeyError) as e:
        _stop(str(e))
    except ImportError as e:
        _stop(f"{e}\n   Or run with --simulate-machines to use simulated printers.")
    if args.simulate_robots:
        factory.simulate_robots()
    where = f"http://{'localhost' if args.host in {'127.0.0.1', '0.0.0.0'} else args.host}:{args.port}"
    print(f"Factory '{factory.config.get('name')}' running")
    print(f"  dashboard: {where}/" + (f"#token={args.token}" if args.token else ""))
    print(f"  model:     {model.model + ' at ' + model.base_url if model else 'none (keywords + rules)'}")
    print(f"  machines:  {', '.join(f'{m.id} ({type(m).__name__})' for m in factory.machines.values())}")
    print(f"  robots:    {len(factory.robots)} registered; agent robots appear online when they send heartbeats")
    print(f"  data:      {args.data_dir} (robots, machines, zones and documents added in the dashboard)")
    print(f"  events:    {args.log}\nCtrl+C to stop.", flush=True)
    serve(factory, args.host, args.port, args.token)


def cmd_factory_request(args) -> None:
    text = " ".join(args.text)
    if not text:
        _stop('Say what you need, e.g.  robofleet factory request "print the actuator housing"')
    print(f"-> asking the factory: {text}")
    result = _client(args).request(text)
    print(f"   {'OK' if result['ok'] else 'NOT DONE'}: {result['summary']}  [{result.get('model', {}).get('latency_s')} s]")
    job = result.get("job")
    if job and job["status"] == "awaiting_confirmation":
        if args.confirm:
            print(f"   {_client(args).confirm(job['id'])['id']} confirmed, starting")
        else:
            print(f"   waiting for confirmation: robofleet factory confirm {job['id']}  (or the Start button on the dashboard)")


def cmd_factory_confirm(args) -> None:
    job = _client(args).confirm(args.job_id)
    print(f"{job['id']}: {job['status']}")


def cmd_factory_status(args) -> None:
    s = _client(args).state()
    print(f"{s['factory']['name']}  (model: {s['factory']['model'] or 'none'})")
    print("\nMACHINES")
    for m in s["machines"]:
        progress = f" {m['progress_pct']}%" if m.get("progress_pct") is not None else ""
        print(f"  {m['id']:<12} {m['state']:<8} {m['zone']:<18}{progress} {m.get('job') or ''}")
    print("\nROBOTS")
    for r in s["robots"]:
        print(f"  {r['id']:<10} {r['status']:<9} {str(r['zone']):<20} {r['battery']:>3}%  {', '.join(r['active_tasks'])}")
    print("\nJOBS")
    for j in s["jobs"][:10]:
        print(f"  {j['id']}  {j['status']:<22} {j['machine_id']} {j['file']} -> {j['deliver_to']}  {j.get('task_id') or ''}")
    print("\nTASKS")
    for t in s["tasks"][:10]:
        print(f"  {t['id']}  {t['status']:<12} {t['robot_id'] or '-':<9} {t['from_zone']} -> {t['to_zone']}")
    print("\nRECENT EVENTS")
    for e in s["events"][:10]:
        print(f"  {time.strftime('%H:%M:%S', time.localtime(e['ts']))}  {e['text']}")


# ---------------------------------------------------------------- robots

def _robot_defaults(client) -> tuple[dict, dict]:
    state = client.state()
    return state["zones"], {r["id"]: r for r in state["robots"]}


def cmd_robot_run(args) -> None:
    from .factory import CommandExecutor, RobotAgent, SimulatedExecutor

    client = _client(args)
    zones, robots = _robot_defaults(client)
    known = robots.get(args.id, {})
    zone = args.zone or known.get("zone") or next(iter(zones))
    executor = CommandExecutor(args.exec) if args.exec else SimulatedExecutor(zones, args.speed)
    agent = RobotAgent(client, args.id, executor, zone=zone, battery=args.battery, name=args.name)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        pass


def cmd_robot_sim(args) -> None:
    from .factory import RobotAgent, SimulatedExecutor

    client = _client(args)
    zones, robots = _robot_defaults(client)
    ids = list(robots)[: args.count] or [f"RBT-{i:03d}" for i in range(1, args.count + 1)]
    ids += [f"RBT-{i:03d}" for i in range(len(ids) + 1, args.count + 1)]
    rng = random.Random(args.seed)
    stop = threading.Event()
    for robot_id in ids:
        zone = robots.get(robot_id, {}).get("zone") or rng.choice(list(zones))
        agent = RobotAgent(client, robot_id, SimulatedExecutor(zones, args.speed), zone=zone,
                           battery=rng.randint(30, 95), interval_s=2.0)
        threading.Thread(target=agent.run_forever, args=(stop,), daemon=True).start()
    print(f"{len(ids)} simulated robots connected to {client.base_url}. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


# ---------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robofleet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    # factory
    factory = sub.add_parser("factory", help="autonomous factory: serve, request, confirm, status")
    fsub = factory.add_subparsers(dest="factory_command", required=True)

    def with_server(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--server", default=os.environ.get("ROBOFLEET_SERVER", DEFAULT_SERVER))
        sp.add_argument("--token", default=os.environ.get("ROBOFLEET_TOKEN"), help="API token (or ROBOFLEET_TOKEN)")
        return sp

    sp = fsub.add_parser("serve", help="run the factory brain (API + dashboard + control loop)")
    sp.add_argument("--config", type=Path, required=True, help="factory file, e.g. examples/actuator_factory/factory.json")
    sp.add_argument("--simulate-machines", action="store_true", help="use simulated printers instead of real ones")
    sp.add_argument("--simulate-robots", action="store_true", help="bring every robot in the factory file online as a simulated robot")
    sp.add_argument("--offline", action="store_true", help="no model: keyword matching + rule-based dispatch")
    sp.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to accept robots on the network")
    sp.add_argument("--port", type=int, default=8080)
    sp.add_argument("--token", default=os.environ.get("ROBOFLEET_TOKEN"), help="require this API token")
    sp.add_argument("--log", type=Path, default=Path("logs/factory_events.jsonl"))
    sp.add_argument("--data-dir", type=Path, default=Path("data"),
                    help="where dashboard-added robots, machines, zones and documents are kept (default: data)")
    sp.set_defaults(func=cmd_factory_serve)

    sp = with_server(fsub.add_parser("request", help="ask the factory to do something"))
    sp.add_argument("text", nargs="*")
    sp.add_argument("--confirm", action="store_true", help="also confirm the job (only if the machine is safe to start)")
    sp.set_defaults(func=cmd_factory_request)

    sp = with_server(fsub.add_parser("confirm", help="confirm a job may start"))
    sp.add_argument("job_id")
    sp.set_defaults(func=cmd_factory_confirm)

    with_server(fsub.add_parser("status", help="machines, robots, jobs, tasks")).set_defaults(func=cmd_factory_status)

    # robots
    robot = sub.add_parser("robot", help="connect robots to a factory")
    rsub = robot.add_subparsers(dest="robot_command", required=True)
    sp = with_server(rsub.add_parser("run", help="run the agent for one robot"))
    sp.add_argument("--id", required=True)
    sp.add_argument("--name")
    sp.add_argument("--zone", help="starting zone (default: from the factory file)")
    sp.add_argument("--battery", type=float, default=100)
    sp.add_argument("--exec", help="command per task, e.g. 'bash real/run_fast.sh {instruction}'. Without it: simulated")
    sp.add_argument("--speed", type=float, default=1.0, help="simulated speed, metres per second")
    sp.set_defaults(func=cmd_robot_run)

    sp = with_server(rsub.add_parser("sim", help="run several simulated robots"))
    sp.add_argument("--count", type=int, default=10)
    sp.add_argument("--speed", type=float, default=1.0)
    sp.add_argument("--seed", type=int, default=1)
    sp.set_defaults(func=cmd_robot_sim)

    # single printer
    printer = sub.add_parser("printer", help="one Bambu Lab printer, without the factory")
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

    # planning only
    p = sub.add_parser("plan", help="one fleet planning cycle from files")
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
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except RuntimeError as e:
        _stop(str(e))
    except OSError as e:
        if "Connection refused" in str(e) or "urlopen" in repr(e):
            _stop(f"Cannot reach the factory at {getattr(args, 'server', '?')}: is 'robofleet factory serve' running?")
        raise


if __name__ == "__main__":
    main()
