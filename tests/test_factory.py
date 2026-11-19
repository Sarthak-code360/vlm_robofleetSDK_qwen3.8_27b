import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from robofleet import ModelError
from robofleet.factory import (CommandExecutor, Factory, FactoryClient, RobotAgent, SimulatedExecutor,
                               SimulatedPrinter, load_factory, make_server)

EXAMPLE = Path(__file__).parent.parent / "examples/rocket_factory/factory.json"

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

    def chat_json(self, messages, schema, name, timeout=None, max_tokens=None):
        self.calls.append(name)
        if self.fail:
            raise ModelError("model offline")
        answer = self.action if name == "factory_action" else self.plan
        if answer is None:
            raise ModelError(f"no answer for {name}")
        return dict(answer), {"model": "fake", "latency_s": 0, "usage": {}}


def make_factory(model=None, data_dir=None, **overrides):
    config = copy.deepcopy({**CONFIG, **overrides})
    machines = {m["id"]: SimulatedPrinter(m, Path(".")) for m in config["machines"]}
    return Factory(config, machines, model, data_dir=data_dir, base_dir=Path("."))


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


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
        self.assertEqual(len(factory.machines), 7)
        self.assertEqual(factory.machines["CNC-1"].type, "cnc_machine")
        self.assertEqual(factory.match_product("please make 2 rockets"), "Sounding Rocket")
        self.assertIsNone(factory.match_product("print a fin"))

    def test_rocket_is_built_in_order(self):
        factory = load_factory(EXAMPLE, simulate_machines=True)
        for m in factory.machines.values():
            m.print_seconds = 0
        factory.sim_speed_m_s, factory.sim_handling_s, factory.sim_interval_s = 1000, 0, 0.02
        factory.simulate_robots()
        try:
            self.assertTrue(wait_for(lambda: sum(r["status"] == "idle" for r in factory.robots.values()) == 10))
            run = factory.start_run({"name": "Sounding Rocket", "target": 1, "approve_each_job": False})
            self.assertEqual(run["parts"]["rocket_fin.gcode.3mf"], 4)
            self.assertEqual(run["route"], {"1": "Final Assembly", "2": "Paint Booth", "3": "Quality Lab", "4": "Shipping Dock"})
            for _ in range(600):
                factory.tick()
                if factory.runs[run["id"]]["status"] == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(factory.runs[run["id"]]["status"], "completed", factory.events[-5:])
            jobs = sorted(factory.jobs.values(), key=lambda j: j["created"])
            assembly = next(j for j in jobs if j["file"] == "rocket_final_assembly")
            parts = [j for j in jobs if factory.runs[run["id"]]["stages"][j["file"]] == 1]
            self.assertEqual(len(parts), 10)  # nose cone, 4 fins, avionics bay, engine mount, nozzle, tank, body tube
            self.assertTrue(all(j["finished"] <= assembly["created"] for j in parts))  # assembly waited for every part
            self.assertTrue(all(j["deliver_to"] == "Final Assembly" for j in parts))
            order = [j["file"] for j in jobs[-3:]]
            self.assertEqual(order, ["rocket_final_assembly", "rocket_paint_and_livery", "rocket_inspection"])
            self.assertEqual(jobs[-1]["deliver_to"], "Shipping Dock")
        finally:
            factory.stop_simulated_robots()


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


class DashboardRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_simulated_robots_and_they_do_the_work(self):
        factory = make_factory(None, data_dir=self.data, robots=[])
        factory.sim_speed_m_s, factory.sim_handling_s = 1000, 0
        added = factory.add_robots({"name": "Mobile Manipulator", "kind": "mobile_manipulator", "home_zone": "Park",
                                    "connection": "simulated", "count": 3})
        self.assertEqual([r["id"] for r in added], ["RBT-001", "RBT-002", "RBT-003"])
        self.assertEqual(added[0]["name"], "Mobile Manipulator 001")
        self.assertTrue(wait_for(lambda: all(r["status"] == "idle" for r in factory.robots.values())))
        try:
            factory.add_machine({"id": "PRINTER-2", "name": "Twin printer", "zone": "Print Bay", "driver": "simulated",
                                 "files": "bracket.gcode.3mf, cap.gcode.3mf", "print_seconds": 1})
            factory.require_confirmation = False
            result = factory.submit_request("print the bracket")
            self.assertTrue(result["ok"], result)
            job_id = result["job"]["id"]
            for _ in range(200):
                factory.tick()
                if factory.jobs[job_id]["status"] == "delivered":
                    break
                time.sleep(0.05)
            self.assertEqual(factory.jobs[job_id]["status"], "delivered", factory.events[-5:])
        finally:
            factory.stop_simulated_robots()

    def test_validation(self):
        factory = make_factory(None, data_dir=self.data)
        bad = [
            ({"id": "RBT-001", "home_zone": "Park"}, "already exists"),
            ({"id": "bad id!", "home_zone": "Park"}, "robot ID"),
            ({"id": "X-1", "home_zone": "Moon"}, "unknown zone"),
            ({"id": "X-1", "home_zone": "Park", "kind": "tank"}, "kind"),
            ({"home_zone": "Park", "count": 99}, "count"),
        ]
        for spec, message in bad:
            with self.assertRaisesRegex(ValueError, message):
                factory.add_robots(spec)
        with self.assertRaisesRegex(ValueError, "ip must be"):
            factory.add_machine({"id": "P-9", "zone": "Print Bay", "driver": "bambu",
                                 "ip": "not-an-ip", "serial": "ABC123", "access_code": "1234"})
        with self.assertRaisesRegex(ValueError, "at least one job"):
            factory.add_machine({"id": "P-9", "zone": "Print Bay", "driver": "simulated", "files": "../x"})
        with self.assertRaisesRegex(ValueError, "factory file"):
            factory.remove_robot("RBT-001")
        with self.assertRaisesRegex(ValueError, "factory file"):
            factory.remove_machine("PRINTER-1")

    def test_registry_survives_restart_and_hides_access_code(self):
        factory = make_factory(None, data_dir=self.data, simulate_machines=True)
        factory.add_zone("Inspection", 4, 4)
        factory.add_robots({"id": "ARM-1", "name": "SO-101 Arm", "kind": "robotic_arm", "home_zone": "Inspection",
                            "connection": "agent"})
        factory.add_machine({"id": "PRINTER-2", "zone": "Inspection", "driver": "bambu", "ip": "192.168.1.40",
                             "serial": "03900ABC", "access_code": "s3cret"})
        state = json.dumps(factory.state())
        self.assertNotIn("s3cret", state)
        self.assertEqual(oct((self.data / "registry.json").stat().st_mode & 0o777), "0o600")

        again = make_factory(None, data_dir=self.data, simulate_machines=True)
        self.assertIn("Inspection", again.site["zones"])
        self.assertEqual(again.robots["ARM-1"]["kind"], "robotic_arm")
        self.assertEqual(again.robots["ARM-1"]["source"], "dashboard")
        self.assertIn("PRINTER-2", again.machines)
        again.remove_robot("ARM-1")
        again.remove_machine("PRINTER-2")
        third = make_factory(None, data_dir=self.data)
        self.assertNotIn("ARM-1", third.robots)
        self.assertNotIn("PRINTER-2", third.machines)

    def test_attachment_name_does_not_steer_keyword_matching(self):
        factory = make_factory(None, data_dir=self.data)
        doc = factory.add_document("Actuator Housing SOP.pdf", PDF)
        result = factory.submit_request("print the actuator gear", document_id=doc["id"])
        self.assertEqual(result["job"]["file"], "actuator_gear.gcode.3mf")
        self.assertEqual(result["job"]["document"]["id"], doc["id"])

    def test_cancel_job(self):
        factory = make_factory(FakeModel(PRINT_HOUSING))
        job = factory.submit_request("print it")["job"]
        self.assertEqual(factory.cancel_job(job["id"])["status"], "cancelled")
        self.assertTrue(factory.submit_request("print it")["ok"])  # the machine is free again
        with self.assertRaises(ValueError):
            factory.cancel_job(job["id"])

    def test_documents_reach_the_robot_over_http(self):
        factory = make_factory(FakeModel(PRINT_HOUSING), data_dir=self.data, require_confirmation=False,
                               robots=[{"id": "RBT-001", "home_zone": "Park"}])
        stop = threading.Event()
        httpd = make_server(factory, "127.0.0.1", 0, token="secret")
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=factory.run_forever, args=(0.05, stop), daemon=True).start()
        try:
            upload = urllib.request.Request(url + "/api/documents", data=PDF, method="POST", headers={
                "Authorization": "Bearer secret", "Content-Type": "application/pdf", "X-Filename": "Housing%20SOP.pdf"})
            with urllib.request.urlopen(upload) as r:
                doc = json.load(r)
            self.assertEqual((doc["id"], doc["name"]), ("DOC-001", "Housing SOP.pdf"))
            not_pdf = urllib.request.Request(url + "/api/documents", data=b"hello", method="POST",
                                             headers={"Authorization": "Bearer secret"})
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(not_pdf)
            err.exception.close()
            with urllib.request.urlopen(f"{url}/api/documents/DOC-001?token=secret") as r:
                self.assertEqual(r.read(), PDF)

            client = FactoryClient(url, "secret")
            folder = self.data / "robot"
            client.fetch_document = lambda document, f=None, _orig=client.fetch_document: _orig(document, folder)
            agent = RobotAgent(client, "RBT-001", CommandExecutor("cat {document} > /dev/null && echo got {document}"),
                               zone="Park", battery=80, interval_s=0.05, verbose=False)
            threading.Thread(target=agent.run_forever, args=(stop,), daemon=True).start()

            result = client.call("/api/requests", {"text": "print it", "document_id": "DOC-001",
                                                   "deliver_to": "Park", "priority": "urgent"})
            self.assertTrue(result["ok"], result)
            job_id = result["job"]["id"]
            self.assertTrue(wait_for(lambda: factory.jobs[job_id]["status"] == "delivered", 10), factory.events[-5:])
            task = factory.tasks[factory.jobs[job_id]["task_id"]]
            self.assertEqual((task["to_zone"], task["priority"]), ("Park", "urgent"))
            self.assertIn("Housing SOP.pdf", task["instruction"])
            self.assertIn("DOC-001.pdf", task["note"])
            self.assertEqual((folder / "DOC-001.pdf").read_bytes(), PDF)

            robots = client.call("/api/robots", {"name": "Twin", "home_zone": "Dock", "count": 2})["robots"]
            self.assertEqual([r["id"] for r in robots], ["RBT-002", "RBT-003"])
            delete = urllib.request.Request(url + "/api/robots/RBT-002", method="DELETE",
                                            headers={"Authorization": "Bearer secret"})
            urllib.request.urlopen(delete).close()
            self.assertNotIn("RBT-002", factory.robots)
            with urllib.request.urlopen(url + "/") as r:
                self.assertIn(b"Autonomous Factory", r.read())
        finally:
            stop.set()
            factory.stop_simulated_robots()
            httpd.shutdown()
            httpd.server_close()


class MachineFilesTest(unittest.TestCase):
    def test_upload_list_print_and_delete_over_http(self):
        with tempfile.TemporaryDirectory() as tmp:
            machines = [dict(CONFIG["machines"][0], prints_dir=tmp)]
            factory = make_factory(None, machines=machines, require_confirmation=False)
            httpd = make_server(factory, "127.0.0.1", 0)
            url = f"http://127.0.0.1:{httpd.server_address[1]}"
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            def call(path, data=None, method=None, headers=None):
                req = urllib.request.Request(url + path, data=data, method=method, headers=headers or {})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.load(r)
                except urllib.error.HTTPError as e:
                    with e:
                        return e.code, json.load(e)
            try:
                sliced = b"PK\x03\x04" + b"0" * 2000
                code, body = call("/api/machines/PRINTER-1/files", sliced, "POST", {"X-Filename": "Bracket%20v2.gcode.3mf"})
                self.assertEqual((code, body["name"], body["size"]), (200, "Bracket v2.gcode.3mf", 2004))
                self.assertEqual(call("/api/machines/PRINTER-1/files", b"hello", "POST", {"X-Filename": "x.gcode.3mf"})[0], 400)
                self.assertEqual(call("/api/machines/PRINTER-1/files", sliced, "POST", {"X-Filename": "x.stl"})[0], 400)
                self.assertFalse(list(Path(tmp).glob(".upload-*")))  # no leftovers from rejected uploads

                files = call("/api/machines/PRINTER-1/files")[1]["files"]
                self.assertEqual({f["name"]: f["deletable"] for f in files},
                                 {"Bracket v2.gcode.3mf": True, "actuator_housing.gcode.3mf": False, "actuator_gear.gcode.3mf": False})

                code, job = call("/api/machines/PRINTER-1/print", json.dumps({"file": "Bracket v2.gcode.3mf"}).encode(), "POST",
                                 {"Content-Type": "application/json"})
                self.assertEqual((code, job["file"]), (200, "Bracket v2.gcode.3mf"))
                self.assertTrue(wait_for(lambda: factory.jobs[job["id"]]["status"] == "running"))
                self.assertEqual(call("/api/machines/PRINTER-1/files/Bracket%20v2.gcode.3mf", method="DELETE")[0], 400)  # printing
                factory.tick()  # print_seconds=0: done, part waits for a robot
                factory.jobs[job["id"]]["status"] = "delivered"
                self.assertEqual(call("/api/machines/PRINTER-1/files/Bracket%20v2.gcode.3mf", method="DELETE")[0], 200)
                self.assertFalse((Path(tmp) / "Bracket v2.gcode.3mf").exists())
            finally:
                httpd.shutdown()
                httpd.server_close()


class ShowcaseTest(unittest.TestCase):
    def test_static_files_and_showcase_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            glb = Path(tmp) / "rocket.glb"
            glb.write_bytes(b"glTF" + b"\0" * 100)
            factory = make_factory(None, showcase={"path": str(glb), "title": "Sounding Rocket"})
            self.assertEqual(factory.state()["factory"]["showcase"]["title"], "Sounding Rocket")
            self.assertIsNone(make_factory(None).state()["factory"]["showcase"])  # no model -> Live card
            httpd = make_server(factory, "127.0.0.1", 0, token="secret")
            url = f"http://127.0.0.1:{httpd.server_address[1]}"
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(url + "/static/three/build/three.module.min.js") as r:
                    self.assertIn("javascript", r.headers["Content-Type"])
                for bad in ("/static/../server.py", "/static/%2e%2e/runtime.py", "/static/nope.js"):
                    with self.assertRaises(urllib.error.HTTPError) as e:
                        urllib.request.urlopen(url + bad)
                    self.assertEqual(e.exception.code, 404); e.exception.close()
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(url + "/api/showcase")  # token required
                self.assertEqual(e.exception.code, 401); e.exception.close()
                with urllib.request.urlopen(url + "/api/showcase?token=secret") as r:
                    self.assertEqual((r.headers["Content-Type"], r.read()[:4]), ("model/gltf-binary", b"glTF"))
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_example_points_at_the_rocket_model(self):
        factory = load_factory(EXAMPLE, simulate_machines=True)
        self.assertTrue(factory.showcase["path"].endswith("model/rocket_assembly.glb"))


class MachineCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_catalog_types_and_auto_machines(self):
        from robofleet.factory.catalog import MACHINE_CATALOG
        for key in ("3d_printer", "cnc_mill", "cnc_lathe", "lathe", "drill_press", "laser_cutter", "fiber_laser",
                    "spot_welder", "press_brake", "injection_molding", "waterjet", "packaging_station"):
            self.assertIn(key, MACHINE_CATALOG)
        factory = make_factory(None, data_dir=self.data)
        laser = factory.ensure_machine("laser_cutter")
        self.assertEqual((laser["id"], laser["live"], laser["type_label"]), ("LASER-001", False, "CO2 Laser Cutter / Engraver"))
        self.assertEqual(factory.ensure_machine("laser_cutter")["id"], "LASER-001")  # reused, not duplicated
        self.assertEqual(factory.ensure_machine("3d_printer")["id"], "PRINTER-1")    # the existing printer
        with self.assertRaises(ValueError):
            factory.ensure_machine("time_machine")

    def test_upload_to_simulated_machine_checks_file_type(self):
        factory = make_factory(None, data_dir=self.data)
        mid = factory.ensure_machine("laser_cutter")["id"]
        good, bad = self.data / "panel.dxf", self.data / "panel.exe"
        good.write_text("0\nSECTION"); bad.write_text("x")
        self.assertEqual(factory.add_machine_file(mid, "panel.dxf", good)["name"], "panel.dxf")
        with self.assertRaisesRegex(ValueError, "takes"):
            factory.add_machine_file(mid, "panel.exe", bad)
        self.assertIn("panel.dxf", factory.machines[mid].list_files())

    def test_run_from_chosen_machines_with_inputs(self):
        factory = make_factory(None, data_dir=self.data, robots=[])
        factory.sim_speed_m_s, factory.sim_handling_s, factory.sim_interval_s = 1000, 0, 0.02
        factory.add_robots({"name": "Rover", "home_zone": "Park", "count": 2})
        mid = factory.ensure_machine("spot_welder")["id"]
        factory.machines[mid].print_seconds = 0
        with self.assertRaisesRegex(ValueError, "Pack layout is required"):
            factory.start_run({"name": "Battery", "target": 1, "steps": [{"machine_id": mid, "inputs": {}}]})
        with self.assertRaisesRegex(ValueError, "upload it first"):
            factory.start_run({"name": "Duck", "target": 1, "steps": [{"type": "3d_printer", "inputs": {"file": "duck.gcode.3mf"}}]})
        run = factory.start_run({"name": "Battery pack", "target": 2, "approve_each_job": False, "steps": [
            {"type": "3d_printer", "inputs": {"file": "actuator_gear.gcode.3mf"}},
            {"machine_id": mid, "inputs": {"pack": "4S2P", "cell": "21700", "pulses": "2"}}]})
        self.assertEqual(run["parts"], {"actuator_gear.gcode.3mf": 1, "4S2P": 1})
        self.assertEqual(run["steps"][1]["inputs"]["pulses"], 2.0)
        self.assertEqual(sorted(run["machines"]), sorted(["PRINTER-1", mid]))
        try:
            for _ in range(300):
                factory.tick()
                if factory.runs[run["id"]]["status"] == "completed":
                    break
                time.sleep(0.02)
            self.assertEqual(factory.runs[run["id"]]["status"], "completed", factory.events[-4:])
        finally:
            factory.stop_simulated_robots()


class ChatPrintingTest(unittest.TestCase):
    def test_print_unknown_thing_reports_printers_and_asks_for_file(self):
        factory = make_factory(None)
        reply = factory.chat_message("can you print a duck")["messages"][-1]["text"]
        self.assertIn("is idle and ready", reply)
        self.assertIn("print file for the duck", reply)
        self.assertIn(".gcode.3mf", reply)
        self.assertFalse(factory.jobs)  # nothing started

    def test_attached_model_prints_when_printer_is_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            machines = [dict(CONFIG["machines"][0], prints_dir=tmp, print_seconds=5)]
            factory = make_factory(None, machines=machines, require_confirmation=False)
            (Path(tmp) / "duck.gcode.3mf").write_bytes(b"PK\x03\x04")
            reply = factory.chat_message("print this", machine_id="PRINTER-1", print_file="duck.gcode.3mf")["messages"][-1]["text"]
            self.assertIn("Printing **duck** now", reply)
            self.assertTrue(wait_for(lambda: any(j["status"] == "running" for j in factory.jobs.values())))
            busy = factory.chat_message("print this", print_file="duck.gcode.3mf")["messages"][-1]["text"]
            self.assertIn("busy printing duck", busy)  # the printer is taken: report, do not queue silently

    def test_owner_instructions_reach_the_model(self):
        seen = {}

        class Spy(FakeModel):
            def chat_json(self, messages, schema, name, timeout=None, max_tokens=None):
                seen["system"] = messages[0]["content"]
                return {"action": "answer", "reply": "4"}, {"latency_s": 0}

        factory = make_factory(Spy(), assistant_instructions=["Our printers use PLA only."])
        self.assertEqual(factory.chat_message("what is 2+2")["messages"][-1]["text"], "4")
        self.assertIn("Our printers use PLA only.", seen["system"])


class SimulateRobotsTest(unittest.TestCase):
    def test_file_robots_come_online(self):
        factory = make_factory(None)
        self.assertEqual(sorted(factory.simulate_robots()), ["RBT-001", "RBT-002"])
        try:
            self.assertTrue(wait_for(lambda: all(r["status"] == "idle" for r in factory.robots.values())))
            self.assertEqual(factory.simulate_robots(), [])  # already running
        finally:
            factory.stop_simulated_robots()


class ProductionRunTest(unittest.TestCase):
    def deliver_all(self, factory, robot="RBT-001"):
        """Plays the robot: picks up every task it is given and reports it done."""
        factory.heartbeat(robot, {"zone": "Park", "battery": 90, "status": "idle"})
        for task in list(factory.tasks.values()):
            if task["robot_id"] == robot and task["status"] == "in_progress":
                factory.complete_task(task["id"], robot, ok=True)

    def test_run_makes_target_units_then_completes(self):
        factory = make_factory(None)
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 90, "status": "idle"})
        run = factory.start_run({"name": "Actuator", "target": 2, "approve_each_job": False,
                                 "parts": {"actuator_housing.gcode.3mf": 1, "actuator_gear.gcode.3mf": 1}})
        self.assertEqual((run["status"], run["units"], run["progress_pct"]), ("running", 0, 0))
        with self.assertRaisesRegex(ValueError, "already active"):
            factory.start_run({"name": "Other", "target": 1, "parts": {"actuator_gear.gcode.3mf": 1}})
        for _ in range(100):
            factory.tick()
            self.deliver_all(factory)
            if factory.runs[run["id"]]["status"] == "completed":
                break
            time.sleep(0.01)
        view = factory.run_view(factory.runs[run["id"]])
        self.assertEqual(view["status"], "completed", factory.events[-5:])
        self.assertEqual((view["units"], view["progress_pct"]), (2, 100.0))
        self.assertEqual(view["jobs_total"], 4)  # exactly 2 housings + 2 gears, nothing extra
        self.assertEqual(sum(1 for j in factory.jobs.values() if j["status"] == "delivered"), 4)

    def test_one_machine_never_holds_two_parts(self):
        factory = make_factory(None)
        factory.start_run({"name": "Actuator", "target": 3, "approve_each_job": False,
                           "parts": {"actuator_housing.gcode.3mf": 1}})
        for _ in range(5):
            factory.tick()
            time.sleep(0.02)
        # no robot online: the finished part stays on the printer, so no second job may start
        self.assertEqual(len(factory.jobs), 1)

    def test_approval_mode_and_stop(self):
        factory = make_factory(None)
        run = factory.start_run({"name": "Actuator", "target": 5, "approve_each_job": True,
                                 "parts": {"actuator_gear.gcode.3mf": 1}})
        factory.tick()
        job = next(iter(factory.jobs.values()))
        self.assertEqual((job["status"], job["run_id"]), ("awaiting_confirmation", run["id"]))
        self.assertEqual(factory.set_run_status(run["id"], "pause")["status"], "paused")
        self.assertEqual(factory.set_run_status(run["id"], "resume")["status"], "running")
        self.assertEqual(factory.set_run_status(run["id"], "stop")["status"], "stopped")
        self.assertEqual(job["status"], "cancelled")
        with self.assertRaises(ValueError):
            factory.set_run_status(run["id"], "resume")

    def test_parts_are_planned_from_the_product_name(self):
        plan = {"parts": [{"file": "actuator_housing.gcode.3mf", "quantity": 1},
                          {"file": "actuator_gear.gcode.3mf", "quantity": 2}]}
        factory = make_factory(FakeModel(plan=plan))  # FakeModel answers every non-request question with .plan
        run = factory.start_run({"name": "Actuator", "target": 10, "description": "full actuator"})
        self.assertEqual(run["parts"], {"actuator_housing.gcode.3mf": 1, "actuator_gear.gcode.3mf": 2})
        offline = make_factory(None)
        run = offline.start_run({"name": "Actuator gear", "target": 1})
        self.assertEqual(run["parts"], {"actuator_gear.gcode.3mf": 1})
        with self.assertRaisesRegex(ValueError, "no machine can make"):
            make_factory(None).start_run({"name": "X", "target": 1, "parts": {"rocket.gcode.3mf": 1}})


class ChatTest(unittest.TestCase):
    def test_print_from_chat_starts_and_robot_is_auto_assigned(self):
        factory = make_factory(None, require_confirmation=False)
        factory.heartbeat("RBT-001", {"zone": "Park", "battery": 90, "status": "idle"})
        out = factory.chat_message("please print the actuator gear")
        reply = out["messages"][-1]
        self.assertEqual(reply["role"], "assistant")
        self.assertIn("Starting the actuator gear", reply["text"])
        job = next(iter(factory.jobs.values()))
        self.assertEqual((job["file"], job["origin"]), ("actuator_gear.gcode.3mf", "chat"))
        self.assertTrue(wait_for(lambda: job["status"] == "running"))
        factory.tick()
        task = factory.tasks[job["task_id"]]
        self.assertEqual(task["robot_id"], "RBT-001")
        texts = [m["text"] for m in factory.chat]
        self.assertIn("The actuator gear is ready. Sending a robot to collect it.", texts)  # progress in plain words
        self.assertIn("RBT-001 is on the way to collect the actuator gear.", texts)

    def test_chosen_robot_and_machine_are_used(self):
        factory = make_factory(None, require_confirmation=False)
        for rid, zone in (("RBT-001", "Park"), ("RBT-002", "Far")):
            factory.heartbeat(rid, {"zone": zone, "battery": 90, "status": "idle"})
        factory.chat_message("print the actuator housing", machine_id="PRINTER-1", robot_id="RBT-002")
        job = next(iter(factory.jobs.values()))
        self.assertTrue(wait_for(lambda: job["status"] == "running"))
        factory.tick()
        self.assertEqual(factory.tasks[job["task_id"]]["robot_id"], "RBT-002")  # not the nearer RBT-001

    def test_alert_when_no_robot_can_collect(self):
        factory = make_factory(None, require_confirmation=False)
        reply = factory.chat_message("print the actuator gear")["messages"][-1]["text"]
        self.assertIn("No robots are online", reply)
        job = next(iter(factory.jobs.values()))
        self.assertTrue(wait_for(lambda: job["status"] == "running"))
        factory.tick()
        alerts = [m for m in factory.chat if m["kind"] == "alert"]
        self.assertTrue(alerts and "Add a robot" in alerts[-1]["text"], factory.chat)
        # the printer still holds the part, so a second print is refused
        second = factory.chat_message("print the actuator gear")["messages"][-1]["text"]
        self.assertIn("busy", second)

    def test_no_machines_and_questions(self):
        empty = make_factory(None, machines=[])
        self.assertIn("no machines", empty.chat_message("print a gear")["messages"][-1]["text"])
        factory = make_factory(None)
        self.assertIn("robots online", factory.chat_message("what is the status?")["messages"][-1]["text"])

    def test_model_reply_and_production_run(self):
        answer = {"action": "start_production", "machine_id": "none", "file": "none", "robot_id": "none",
                  "from_zone": "none", "to_zone": "none", "product": "Actuator gear", "quantity": 5,
                  "reply": "On it - 5 actuator gears coming up. Is there anything else you'd like to know about the factory?"}

        class ChatModel(FakeModel):
            def chat_json(self, messages, schema, name, timeout=None, max_tokens=None):
                if name == "factory_chat":
                    self.calls.append(name)
                    return dict(answer), {"model": "fake", "latency_s": 0.1}
                return super().chat_json(messages, schema, name, timeout)

        factory = make_factory(ChatModel(plan={"parts": [{"file": "actuator_gear.gcode.3mf", "quantity": 1}]}),
                               require_confirmation=False)
        reply = factory.chat_message("make 5 actuator gears")["messages"][-1]["text"]
        self.assertIn("On it", reply)
        self.assertNotIn("anything else", reply)  # the chat-model closing line is removed
        run = next(iter(factory.runs.values()))
        self.assertEqual((run["target"], run["parts"], run["origin"]), (5, {"actuator_gear.gcode.3mf": 1}, "chat"))
        self.assertIn("factory_chat", factory.model.calls)


if __name__ == "__main__":
    unittest.main()
