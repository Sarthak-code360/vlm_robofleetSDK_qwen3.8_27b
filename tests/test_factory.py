import threading
import time
import unittest
from pathlib import Path

from robofleet import ModelError
from robofleet.factory import (CommandExecutor, Factory, FactoryClient, RobotAgent, SimulatedExecutor,
                               SimulatedPrinter, load_factory, make_server)

EXAMPLE = Path(__file__).parent.parent / "examples/actuator_factory/factory.json"

CONFIG = {
    "name": "Test factory", "type": "test factory",
    "zones": {"Print Bay": {"x": 0, "y": 0}, "Output Box": {"x": 10, "y": 0},
              "Park": {"x": 0, "y": 5}, "Far": {"x": 50, "y": 50}, "Dock": {"x": 10, "y": 5}},
    "chargers": ["Dock"], "default_output_zone": "Output Box",
    "rules": {"battery_reserve_pct": 20, "charge_threshold_pct": 25, "max_queued_tasks_per_robot": 1, "task_battery_cost": 5},
    "require_confirmation": True, "robot_timeout_s": 15,
    "machines": [{"id": "PRINTER-1", "type": "3d_printer", "zone": "Print Bay", "print_seconds": 0,
                  "prints_dir": "does-not-exist", "files": ["actuator_housing.gcode.3mf", "actuator_gear.gcode.3mf"]}],
    "robots": [{"id": "RBT-001", "home_zone": "Park"}, {"id": "RBT-002", "home_zone": "Far"}],
}

PRINT_HOUSING = {"action": "run_machine", "machine_id": "PRINTER-1", "file": "actuator_housing.gcode.3mf",
                 "from_zone": "none", "to_zone": "Output Box", "reason": "user wants the housing"}


class FakeModel:
    """Answers the factory's two questions: what to do (factory_action) and who does it (fleet_plan)."""

    model = "fake"

    def __init__(self, action=None, plan=None, fail=False):
        self.action, self.plan, self.fail, self.calls = action, plan, fail, []

    def chat_json(self, messages, schema, name, timeout=None):
        self.calls.append(name)
        if self.fail:
            raise ModelError("model offline")
        answer = self.action if name == "factory_action" else self.plan
        if answer is None:
            raise ModelError(f"no answer for {name}")
        return dict(answer), {"model": "fake", "latency_s": 0, "usage": {}}


def make_factory(model=None, **overrides):
    config = {**CONFIG, **overrides}
    machines = {m["id"]: SimulatedPrinter(m, Path(".")) for m in config["machines"]}
    return Factory(config, machines, model)


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class FactoryFlowTest(unittest.TestCase):
    def test_print_then_robot_collects(self):
        plan = {"assignments": [{"task_id": "TASK-0002", "robot_id": "RBT-001", "reason": "closest, idle"}],
                "robot_messages": [], "summary": ""}
        factory = make_factory(FakeModel(PRINT_HOUSING, plan))
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 80, "status": "idle"})
        factory.heartbeat("RBT-002", {"zone": "Far", "battery": 90, "status": "idle"})

        result = factory.submit_request("print the actuator housing")
        self.assertTrue(result["ok"], result)
        job_id = result["job"]["id"]
        self.assertEqual(factory.jobs[job_id]["status"], "awaiting_confirmation")

        factory.tick()  # nothing may start before a person confirms
        self.assertEqual(factory.jobs[job_id]["status"], "awaiting_confirmation")

        factory.confirm_job(job_id)
        self.assertTrue(wait_for(lambda: factory.jobs[job_id]["status"] == "running"))
        factory.tick()  # print finished (print_seconds=0) -> collect task -> dispatched

        job = factory.jobs[job_id]
        task = factory.tasks[job["task_id"]]
        self.assertEqual(job["status"], "done")
        self.assertEqual((task["from_zone"], task["to_zone"]), ("Print Bay", "Output Box"))
        self.assertEqual((task["status"], task["robot_id"]), ("assigned", "RBT-001"))
        self.assertEqual(factory.model.calls, ["factory_action", "fleet_plan"])

        self.assertIsNone(factory.heartbeat("RBT-002", {"zone": "Far", "battery": 90})["task"])
        delivered = factory.heartbeat("RBT-001", {"zone": "Park", "battery": 80})["task"]
        self.assertEqual(delivered["id"], task["id"])
        self.assertIn("actuator_housing", delivered["instruction"])
        self.assertIsNone(factory.heartbeat("RBT-001", {"zone": "Park", "battery": 80})["task"])  # sent once

        factory.complete_task(task["id"], "RBT-001", ok=True)
        self.assertEqual(factory.jobs[job_id]["status"], "delivered")

    def test_model_choices_are_checked(self):
        bad_file = dict(PRINT_HOUSING, file="rocket.gcode.3mf")
        self.assertIn("not a file", make_factory(FakeModel(bad_file)).submit_request("x")["summary"])
        bad_machine = dict(PRINT_HOUSING, machine_id="CNC-9")
        self.assertIn("unknown machine", make_factory(FakeModel(bad_machine)).submit_request("x")["summary"])

        factory = make_factory(FakeModel(PRINT_HOUSING))
        self.assertTrue(factory.submit_request("print it")["ok"])
        second = factory.submit_request("print it again")
        self.assertFalse(second["ok"])
        self.assertIn("already has a job", second["summary"])

    def test_unsafe_model_dispatch_is_rejected(self):
        # The model tries to send a robot whose battery would drop below the reserve.
        plan = {"assignments": [{"task_id": "TASK-0001", "robot_id": "RBT-001", "reason": "x"}],
                "robot_messages": [], "summary": ""}
        factory = make_factory(FakeModel(plan=plan))
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 22, "status": "idle"})
        factory._new_task("Print Bay", "Output Box", "move it")
        factory.dispatch()
        self.assertEqual(factory.tasks["TASK-0001"]["status"], "pending")
        self.assertTrue(any("battery would drop" in e["text"] for e in factory.events))

    def test_model_offline_falls_back_to_keywords_and_rules(self):
        factory = make_factory(FakeModel(fail=True), require_confirmation=False)
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 80, "status": "idle"})
        factory.heartbeat("RBT-002", {"zone": "Far", "battery": 90, "status": "idle"})
        result = factory.submit_request("please print the actuator gear")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["job"]["file"], "actuator_gear.gcode.3mf")
        job_id = result["job"]["id"]
        self.assertTrue(wait_for(lambda: factory.jobs[job_id]["status"] == "running"))
        factory.tick()
        task = factory.tasks[factory.jobs[job_id]["task_id"]]
        self.assertEqual(task["robot_id"], "RBT-001")  # nearest, chosen by the rules

    def test_nothing_matches(self):
        result = make_factory(None).submit_request("weld the rocket fins")
        self.assertFalse(result["ok"])

    def test_offline_robot_loses_its_task(self):
        factory = make_factory(None)
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 80, "status": "idle"})
        factory._new_task("Print Bay", "Output Box", "move it")
        factory.dispatch()
        self.assertEqual(factory.tasks["TASK-0001"]["robot_id"], "RBT-001")
        factory.robots["RBT-001"]["last_seen"] -= 60
        factory.tick()
        self.assertEqual(factory.robots["RBT-001"]["status"], "offline")
        self.assertEqual(factory.tasks["TASK-0001"]["status"], "pending")

    def test_unknown_robot_registers_itself(self):
        factory = make_factory(None)
        factory.heartbeat("NEW-7", {"zone": "Dock", "battery": 50, "status": "idle", "name": "New arm"})
        self.assertEqual(factory.robots["NEW-7"]["name"], "New arm")
        factory.heartbeat("NEW-7", {"zone": "Mars", "battery": 50})
        self.assertEqual(factory.robots["NEW-7"]["zone"], "Dock")  # unknown zones are ignored


class ExampleConfigTest(unittest.TestCase):
    def test_example_loads_with_simulated_machines(self):
        factory = load_factory(EXAMPLE, simulate_machines=True)
        self.assertEqual(len(factory.robots), 10)
        self.assertIsInstance(factory.machines["PRINTER-1"], SimulatedPrinter)
        self.assertTrue(factory.machines["PRINTER-1"].list_files())


class CommandExecutorTest(unittest.TestCase):
    def test_instruction_is_passed_safely(self):
        class Robot:
            zone = "Park"

            def log(self, text):
                pass

        tricky = "Collect 'part' from \"Bay\"; rm -rf / && echo $HOME"
        ok, note = CommandExecutor("printf %s {instruction}").run(
            {"id": "TASK-1", "instruction": tricky, "from_zone": "A", "to_zone": "B"}, Robot())
        self.assertTrue(ok)
        self.assertEqual(note, tricky)


class HttpEndToEndTest(unittest.TestCase):
    def test_request_print_and_robot_delivers_over_http(self):
        factory = make_factory(None, robots=[{"id": "RBT-001", "home_zone": "Park"}])
        stop = threading.Event()
        httpd = make_server(factory, "127.0.0.1", 0, token="secret")
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=factory.run_forever, args=(0.05, stop), daemon=True).start()
        try:
            with self.assertRaises(RuntimeError):  # wrong token
                FactoryClient(url, "nope").state()
            client = FactoryClient(url, "secret")
            agent = RobotAgent(client, "RBT-001", SimulatedExecutor(factory.site["zones"], 1000, 0),
                               zone="Park", battery=80, interval_s=0.05, verbose=False)
            threading.Thread(target=agent.run_forever, args=(stop,), daemon=True).start()

            result = client.request("print the actuator housing")
            self.assertTrue(result["ok"], result)
            client.confirm(result["job"]["id"])
            self.assertTrue(wait_for(lambda: factory.jobs[result["job"]["id"]]["status"] == "delivered", 10),
                            client.state()["events"][:5])
            self.assertEqual(agent.zone, "Output Box")
            self.assertEqual(client.state()["robots"][0]["status"], "idle")
        finally:
            stop.set()
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
