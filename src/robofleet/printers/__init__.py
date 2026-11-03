"""3D printer support (Bambu Lab, LAN mode). Needs: pip install "robofleet[printer]"."""

from .bambu import IDLE_STATES, BambuPrinter, start_print_payload
from .cad import CadError, design_part, render_stl
from .checks import check_printer_ready, check_size, stl_size
from .chooser import NO_FILE, choose_print_file, list_print_files
from .slicer import SlicerError, slice_stl

__all__ = [
    "BambuPrinter", "IDLE_STATES", "start_print_payload",
    "check_printer_ready", "check_size", "stl_size",
    "choose_print_file", "list_print_files", "NO_FILE",
    "design_part", "render_stl", "CadError",
    "slice_stl", "SlicerError",
]
