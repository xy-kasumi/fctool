#!/usr/bin/env python3
"""FCStd -> STL conversion core.

Must be executed by FreeCAD's own interpreter, e.g.:

    FreeCADCmd.exe fc2stl_core.py input.FCStd output.stl

(sys.argv is: [FreeCADCmd.exe, <script>, input, output])
"""

import sys

import FreeCAD as App
import Mesh


def convert(input_file: str, output_file: str) -> None:
    doc = App.openDocument(input_file)
    try:
        # Prefer PartDesign Bodies (their Shape is the tip's compound),
        # otherwise take any object that has a solid shape.
        bodies = [o for o in doc.Objects if o.TypeId == "PartDesign::Body"]
        objs = bodies or [
            o
            for o in doc.Objects
            if o.isDerivedFrom("Part::Feature") and not o.Shape.isNull()
        ]
        if not objs:
            sys.exit(f"No shape objects found in {input_file}")

        print("Exporting:", ", ".join(o.Name for o in objs))
        Mesh.export(objs, output_file)
        print("Wrote:", output_file)
    finally:
        App.closeDocument(doc.Name)


convert(sys.argv[2], sys.argv[3])
