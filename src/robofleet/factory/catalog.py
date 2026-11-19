"""Every kind of stationary machine the factory understands, and what each one needs to run a job.

Each entry: label, category, stage (build order), live (a real driver exists), and inputs (the fields the
dashboard asks for when the machine is chosen for production).

Stage order: 1 make parts, 2 assemble, 3 finish (paint, heat treat), 4 inspect, 5 pack. A later stage of a
unit starts only when that unit's earlier-stage parts have been delivered.

Input kinds: file (with ``accept`` extensions), number (with ``unit``), select (with ``options``), text.
The input marked ``job: True`` names the job (usually the uploaded file).
"""

from __future__ import annotations

METALS = ["Aluminium 6061", "Mild steel", "Stainless steel", "Brass", "Copper", "Titanium"]
PLASTICS = ["ABS", "PLA", "PETG", "Nylon", "Polycarbonate", "Delrin (POM)", "Acrylic"]


def _f(label: str, accept: str, required: bool = True) -> dict:
    return {"key": "file", "label": label, "kind": "file", "accept": accept, "required": required, "job": True}


def _n(key: str, label: str, unit: str = "", default=None, required: bool = False) -> dict:
    return {"key": key, "label": label, "kind": "number", "unit": unit, "default": default, "required": required}


def _s(key: str, label: str, options: list, default=None) -> dict:
    return {"key": key, "label": label, "kind": "select", "options": options, "default": default or options[0]}


def _t(key: str, label: str, placeholder: str = "", required: bool = False, job: bool = False) -> dict:
    return {"key": key, "label": label, "kind": "text", "placeholder": placeholder, "required": required, "job": job}


MACHINE_CATALOG: dict[str, dict] = {
    # ---- additive
    "3d_printer": {"label": "3D Printer (FDM)", "category": "Additive", "stage": 1, "live": True,
                   "inputs": [_f("Print file (sliced)", ".gcode.3mf")]},
    "resin_printer": {"label": "Resin 3D Printer (SLA/MSLA)", "category": "Additive", "stage": 1,
                      "inputs": [_f("Sliced resin file", ".ctb,.sl1,.pwmx,.goo"), _s("resin", "Resin", ["Standard", "Tough", "Flexible", "Castable"])]},
    # ---- machining
    "cnc_machine": {"label": "CNC Machine", "category": "Machining", "stage": 1,
                    "inputs": [_f("G-code program", ".nc,.gcode,.tap,.ngc"), _s("material", "Material", METALS + PLASTICS)]},
    "cnc_mill": {"label": "CNC Milling Machine", "category": "Machining", "stage": 1,
                 "inputs": [_f("G-code program", ".nc,.gcode,.tap,.ngc"), _s("material", "Material", METALS + PLASTICS),
                            _t("stock", "Stock size", "e.g. 100 x 60 x 20 mm")]},
    "cnc_router": {"label": "CNC Router", "category": "Machining", "stage": 1,
                   "inputs": [_f("G-code program", ".nc,.gcode,.tap"), _s("material", "Material", ["Plywood", "MDF", "Hardwood", "Acrylic", "Aluminium composite", "Foam"]),
                              _n("thickness", "Sheet thickness", "mm", 12)]},
    "cnc_lathe": {"label": "CNC Lathe", "category": "Machining", "stage": 1,
                  "inputs": [_f("G-code program", ".nc,.gcode,.tap"), _s("material", "Material", METALS + PLASTICS),
                             _n("bar_diameter", "Bar stock diameter", "mm", 25)]},
    "lathe": {"label": "Lathe (turning)", "category": "Machining", "stage": 1,
              "inputs": [_f("Drawing", ".pdf,.dxf,.step,.stp"), _s("material", "Material", METALS + PLASTICS),
                         _n("diameter", "Finished diameter", "mm", 20), _n("length", "Length", "mm", 50)]},
    "drill_press": {"label": "Drilling Machine", "category": "Machining", "stage": 1,
                    "inputs": [_f("Hole pattern (optional)", ".dxf,.csv", required=False), _n("holes", "Number of holes", "", 4, True),
                               _n("hole_diameter", "Hole diameter", "mm", 6, True), _s("material", "Material", METALS + PLASTICS)]},
    "surface_grinder": {"label": "Surface Grinder", "category": "Machining", "stage": 1,
                        "inputs": [_s("material", "Material", METALS), _n("depth", "Depth to remove", "mm", 0.2, True)]},
    "wire_edm": {"label": "Wire EDM", "category": "Machining", "stage": 1,
                 "inputs": [_f("Profile", ".dxf"), _s("material", "Material", METALS), _n("thickness", "Thickness", "mm", 10)]},
    "band_saw": {"label": "Band Saw / Cut-off Saw", "category": "Machining", "stage": 1,
                 "inputs": [_s("material", "Material", METALS + PLASTICS), _n("length", "Cut length", "mm", 100, True),
                            _n("pieces", "Pieces per unit", "", 1)]},
    # ---- cutting
    "laser_cutter": {"label": "CO2 Laser Cutter / Engraver", "category": "Cutting", "stage": 1,
                     "inputs": [_f("Cut file", ".dxf,.svg,.ai,.pdf"), _s("material", "Material", ["Acrylic", "Plywood", "MDF", "Cardboard", "Leather", "Fabric"]),
                                _n("thickness", "Thickness", "mm", 3), _s("mode", "Mode", ["Cut", "Engrave", "Cut + engrave"])]},
    "fiber_laser": {"label": "Fiber Laser Cutter", "category": "Cutting", "stage": 1,
                    "inputs": [_f("Cut file", ".dxf,.svg"), _s("material", "Material", METALS), _n("thickness", "Thickness", "mm", 2)]},
    "plasma_cutter": {"label": "CNC Plasma Cutter", "category": "Cutting", "stage": 1,
                      "inputs": [_f("Cut file", ".dxf"), _s("material", "Material", ["Mild steel", "Stainless steel", "Aluminium 6061"]),
                                 _n("thickness", "Thickness", "mm", 6)]},
    "waterjet": {"label": "Waterjet Cutter", "category": "Cutting", "stage": 1,
                 "inputs": [_f("Cut file", ".dxf"), _s("material", "Material", METALS + ["Glass", "Stone", "Composite"]), _n("thickness", "Thickness", "mm", 10)]},
    "sheet_shear": {"label": "Sheet Metal Shear", "category": "Cutting", "stage": 1,
                    "inputs": [_s("material", "Material", METALS), _n("thickness", "Thickness", "mm", 1.5), _n("length", "Cut length", "mm", 500, True)]},
    # ---- forming / moulding
    "press_brake": {"label": "Press Brake (bending)", "category": "Forming", "stage": 1,
                    "inputs": [_f("Bend drawing", ".dxf,.pdf"), _s("material", "Material", METALS), _n("thickness", "Thickness", "mm", 2),
                               _n("bends", "Number of bends", "", 2)]},
    "hydraulic_press": {"label": "Hydraulic / Stamping Press", "category": "Forming", "stage": 1,
                        "inputs": [_t("die", "Die / tool", "e.g. D-14 bracket die", True, job=True), _n("force", "Force", "t", 20),
                                   _s("material", "Material", METALS)]},
    "injection_molding": {"label": "Injection Moulding Machine", "category": "Forming", "stage": 1,
                          "inputs": [_t("mold", "Mould", "e.g. M-03 cap mould", True, job=True), _s("plastic", "Plastic", ["ABS", "PP", "PE", "Nylon", "Polycarbonate"]),
                                     _n("shots", "Shots per unit", "", 1)]},
    "vacuum_former": {"label": "Vacuum Former", "category": "Forming", "stage": 1,
                      "inputs": [_t("mold", "Mould", "e.g. tray mould", True, job=True), _s("sheet", "Sheet", ["HIPS", "PETG", "ABS", "Acrylic"]),
                                 _n("thickness", "Sheet thickness", "mm", 1.5)]},
    "composite_cell": {"label": "Composite Layup Cell", "category": "Forming", "stage": 1,
                       "inputs": [_f("Ply book (optional)", ".pdf,.dxf", required=False), _s("material", "Material", ["Carbon fibre", "Fibreglass", "Kevlar"]),
                                  _n("layers", "Layers", "", 4)]},
    # ---- joining & electronics
    "spot_welder": {"label": "Spot Welder (battery packs)", "category": "Joining", "stage": 1,
                    "inputs": [_t("pack", "Pack layout", "e.g. 4S2P", True, job=True), _s("cell", "Cell", ["18650", "21700", "26650", "LiFePO4 32700"]),
                               _s("strip", "Nickel strip", ["0.10 mm", "0.15 mm", "0.20 mm", "0.30 mm"], "0.15 mm"), _n("pulses", "Pulses per weld", "", 2)]},
    "welding_station": {"label": "Welding Robot (MIG/TIG)", "category": "Joining", "stage": 1,
                        "inputs": [_f("Weld program (optional)", ".job,.txt,.pdf", required=False), _s("process", "Process", ["MIG", "TIG", "Stick"]),
                                   _s("material", "Material", METALS), _n("wire", "Wire diameter", "mm", 0.8)]},
    "pick_and_place": {"label": "SMT Pick-and-Place", "category": "Joining", "stage": 1,
                       "inputs": [_f("Placement file", ".csv,.pos,.txt"), _f("BOM (optional)", ".csv,.xlsx", required=False) | {"key": "bom", "job": False}]},
    "reflow_oven": {"label": "Reflow Oven (SMT)", "category": "Joining", "stage": 2,
                    "inputs": [_s("profile", "Solder profile", ["Lead-free SAC305", "Leaded Sn63Pb37", "Low-temp SnBi"]), _n("boards", "Boards per unit", "", 1)]},
    "assembly_station": {"label": "Assembly Station", "category": "Assembly", "stage": 2,
                         "inputs": [_f("Assembly instructions (optional)", ".pdf", required=False)]},
    # ---- finishing, test, packing
    "heat_treatment": {"label": "Heat-Treatment Furnace", "category": "Finishing", "stage": 3,
                       "inputs": [_n("temperature", "Temperature", "°C", 520, True), _n("hold", "Hold time", "min", 60, True)]},
    "paint_booth": {"label": "Paint / Powder-Coat Booth", "category": "Finishing", "stage": 3,
                    "inputs": [_t("colour", "Colour", "e.g. RAL 9010 white"), _s("finish", "Finish", ["Gloss", "Satin", "Matte"])]},
    "inspection_station": {"label": "Inspection / CMM", "category": "Quality", "stage": 4,
                           "inputs": [_s("check", "Check", ["Visual", "Dimensional (CMM)", "Leak test", "Electrical test"])]},
    "packaging_station": {"label": "Packaging Station", "category": "Logistics", "stage": 5,
                          "inputs": [_s("packaging", "Packaging", ["Box", "Crate", "Anti-static bag", "Pallet"])]},
}

# id prefixes for machines created automatically
PREFIX = {"3d_printer": "PRINTER", "resin_printer": "RESIN", "cnc_machine": "CNC", "cnc_mill": "MILL", "cnc_router": "ROUTER",
          "cnc_lathe": "CNCLATHE", "lathe": "LATHE", "drill_press": "DRILL", "surface_grinder": "GRIND", "wire_edm": "EDM",
          "band_saw": "SAW", "laser_cutter": "LASER", "fiber_laser": "FIBER", "plasma_cutter": "PLASMA", "waterjet": "WJET",
          "sheet_shear": "SHEAR", "press_brake": "BRAKE", "hydraulic_press": "PRESS", "injection_molding": "MOLD",
          "vacuum_former": "VAC", "composite_cell": "COMP", "spot_welder": "SPOT", "welding_station": "WELD",
          "pick_and_place": "PNP", "reflow_oven": "REFLOW", "assembly_station": "ASSY", "heat_treatment": "HEAT",
          "paint_booth": "PAINT", "inspection_station": "QA", "packaging_station": "PACK"}

STAGE_NAMES = {1: "Make parts", 2: "Assemble", 3: "Finish", 4: "Inspect", 5: "Pack"}


def machine_label(machine_type: str) -> str:
    return MACHINE_CATALOG.get(machine_type, {}).get("label", machine_type.replace("_", " ").title())


def accepted_extensions(machine_type: str) -> list[str]:
    exts = []
    for field in MACHINE_CATALOG.get(machine_type, {}).get("inputs", []):
        if field["kind"] == "file":
            exts += [e.strip().lower() for e in field["accept"].split(",")]
    return exts
