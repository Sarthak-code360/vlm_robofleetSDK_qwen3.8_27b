import struct
import tempfile
import unittest
from pathlib import Path

from robofleet.printers import (NO_FILE, check_printer_ready, check_size, choose_print_file, list_print_files,
                                start_print_payload, stl_size)

ASCII_STL = """solid t
facet normal 0 0 1
 outer loop
  vertex 0 0 0
  vertex 40 0 0
  vertex 0 30 20
 endloop
endfacet
endsolid t
"""


class FakeModel:
    def __init__(self, answer):
        self.answer, self.messages, self.schema = answer, None, None

    def chat_json(self, messages, schema, name, timeout=None):
        self.messages, self.schema = messages, schema
        return dict(self.answer), {"model": "fake", "latency_s": 0, "usage": {}}


class PrinterCheckTest(unittest.TestCase):
    def test_ascii_and_binary_stl_size(self):
        with tempfile.TemporaryDirectory() as d:
            ascii_path = Path(d) / "a.stl"
            ascii_path.write_text(ASCII_STL)
            self.assertEqual(stl_size(ascii_path), (40, 30, 20))

            binary_path = Path(d) / "b.stl"
            tri = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 5, 7, 0)
            binary_path.write_bytes(b"\0" * 80 + struct.pack("<I", 1) + tri)
            self.assertEqual(stl_size(binary_path), (10, 5, 7))

    def test_size_limits(self):
        self.assertEqual(check_size((40, 30, 20), [256, 256, 256]), [])
        self.assertEqual(len(check_size((250, 30, 0), [256, 256, 256])), 2)

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
