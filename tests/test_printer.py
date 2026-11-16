import tempfile
import unittest
from pathlib import Path

from robofleet.printers import NO_FILE, check_printer_ready, choose_print_file, list_print_files, start_print_payload


class FakeModel:
    def __init__(self, answer):
        self.answer, self.messages, self.schema = answer, None, None

    def chat_json(self, messages, schema, name, timeout=None):
        self.messages, self.schema = messages, schema
        return dict(self.answer), {"model": "fake", "latency_s": 0, "usage": {}}


class PrinterCheckTest(unittest.TestCase):
    def test_printer_ready(self):
        self.assertEqual(check_printer_ready({"gcode_state": "IDLE", "print_error": 0}), [])
        self.assertIn("busy", check_printer_ready({"gcode_state": "RUNNING"})[0])

    def test_start_payload(self):
        p = start_print_payload("file:///sdcard/cube.gcode.3mf")["print"]
        self.assertEqual(p["command"], "project_file")
        self.assertEqual(p["param"], "Metadata/plate_1.gcode")
        self.assertEqual(p["subtask_name"], "cube.gcode.3mf")


class ChooserTest(unittest.TestCase):
    def test_lists_only_sliced_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("b.gcode.3mf", "a.gcode.3mf", "model.3mf", "notes.txt"):
                (Path(d) / name).touch()
            self.assertEqual(list_print_files(d), ["a.gcode.3mf", "b.gcode.3mf"])
        self.assertEqual(list_print_files("/no/such/dir"), [])

    def test_model_can_only_pick_listed_files(self):
        model = FakeModel({"file": "cube.gcode.3mf", "reason": "matches"})
        choice, _ = choose_print_file(model, "print the cube", ["cube.gcode.3mf"])
        self.assertEqual(choice["file"], "cube.gcode.3mf")
        self.assertEqual(model.schema["properties"]["file"]["enum"], ["cube.gcode.3mf", NO_FILE])

        made_up = FakeModel({"file": "../../etc/passwd", "reason": "x"})
        self.assertEqual(choose_print_file(made_up, "x", ["cube.gcode.3mf"])[0]["file"], NO_FILE)


if __name__ == "__main__":
    unittest.main()
