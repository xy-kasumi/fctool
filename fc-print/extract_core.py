#!/usr/bin/env python3
"""Body extraction core for fc-print.

Executed by FreeCAD's own interpreter (see InvokeFreeCAD in freecad.go and
fcstd_to_stl.py); never imported into the Go host process:

    FreeCADCmd <this-script> <output_dir> <results_json> <source.FCStd>...

For every source document, each shape object carrying a ``Mfg.print``
string property is parsed, reoriented so the settings' ``up`` axis points
to Z+, and exported to exactly one STL file under <output_dir>. Results are
reported via the JSON file rather than stdout, because FreeCAD prints its
own chatter to stdout:

    {"bodies": [{"input", "name", "label", "settings", "brep", "output"}],
     "errors": [{"input", "body", "error"}]}

``errors`` entries with an empty ``body`` mean the whole document could not
be processed; others are per-body problems (e.g. a malformed Mfg.print).
"""

import hashlib
import json
import os
import sys

import FreeCAD as App
import Mesh

WSP = ' \t\n\x0b\x0c\r'
DIRECTIONS = ('X-', 'X+', 'Y-', 'Y+', 'Z-', 'Z+')
BODY_MODES = ('N', 'S', 'SS')
DEFAULTS = {'mat': 'any', 'nozzle': '0.4', 'body': 'N'}

UP_VECTORS = {
    'X-': (-1, 0, 0), 'X+': (1, 0, 0),
    'Y-': (0, -1, 0), 'Y+': (0, 1, 0),
    'Z-': (0, 0, -1), 'Z+': (0, 0, 1),
}


def split_fields(value):
    """Split on runs of ASCII whitespace (the separator of the EBNF)."""
    fields, cur = [], []
    for ch in value:
        if ch in WSP:
            if cur:
                fields.append(''.join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        fields.append(''.join(cur))
    return fields


def parse_settings(value):
    """Parse Mfg.print; mirrors Go's ParsePrintSettings EBNF exactly."""
    settings = dict(DEFAULTS)
    seen = set()
    for field in split_fields(value):
        key, sep, val = field.partition('=')
        if not sep:
            raise ValueError('bad field %r: expected key=value' % field)
        if not key or not val or '=' in val:
            raise ValueError('bad field %r: invalid key or value' % field)
        if key not in ('mat', 'nozzle', 'up', 'body'):
            raise ValueError('unknown key %r' % key)
        if key in seen:
            raise ValueError('duplicate key %r' % key)
        if key == 'up' and val not in DIRECTIONS:
            raise ValueError('bad up direction %r' % val)
        if key == 'body' and val not in BODY_MODES:
            raise ValueError('bad body mode %r' % val)
        seen.add(key)
        settings[key] = val
    if 'up' not in seen:
        raise ValueError('missing required "up"')
    return settings


def sanitize(name):
    """Neutralize '-' and ' ' so they cannot fake the '-' separators."""
    return name.replace('-', '_').replace(' ', '_')


def target_bodies(doc):
    """Yield objects with a non-null shape and a Mfg.print property."""
    for obj in doc.Objects:
        try:
            if 'print' not in obj.PropertiesList:
                continue
            if obj.getGroupOfProperty('print') != 'Mfg':
                continue
            if not obj.isDerivedFrom('Part::Feature'):
                continue
            if obj.Shape.isNull():
                continue
        except Exception:
            continue
        yield obj


def reorient(shape, up):
    """Rigidly rotate a copy of shape so that up points to Z+."""
    rotated = shape.copy()
    rotation = App.Rotation(App.Vector(*UP_VECTORS[up]), App.Vector(0, 0, 1))
    rotated.Placement = App.Placement(App.Vector(0, 0, 0),
                                      rotation).multiply(rotated.Placement)
    return rotated


def stl_name(settings, source, label, used):
    """<mat><nozzle><body>-<file stem>-<body label>(-<n>).stl; n starts at 2."""
    base = '%s%s%s-%s-%s' % (
        settings['mat'], settings['nozzle'], settings['body'],
        sanitize(os.path.splitext(os.path.basename(source))[0]),
        sanitize(label))
    n = used.get(base, 0) + 1
    used[base] = n
    if n > 1:
        base = '%s-%d' % (base, n)
    return base + '.stl'


def main():
    output_dir, results_path = sys.argv[2], sys.argv[3]
    sources = sys.argv[4:]
    bodies, errors, used = [], [], {}
    for index, source in enumerate(sources):
        try:
            doc = App.openDocument(source)
        except Exception as exc:
            errors.append({'source': index,
                           'error': 'open failed: %s' % exc})
            continue
        try:
            for obj in target_bodies(doc):
                try:
                    settings = parse_settings(
                        str(obj.getPropertyByName('print')))
                except ValueError as exc:
                    errors.append({'source': index, 'body': obj.Name,
                                   'error': str(exc)})
                    continue
                try:
                    shape = obj.Shape
                    # Sameness hash: canonical BREP before reorientation
                    # (see DETAILS-body-sameness.md).
                    brep = hashlib.sha256(
                        shape.exportBrepToString().encode('utf-8')).hexdigest()
                    name = stl_name(settings, source, obj.Label, used)
                    # Export through a temporary object: Mesh.export writes
                    # compact binary STL (exportStl would write ASCII).
                    carrier = doc.addObject('Part::Feature',
                                            'fcprint_export_carrier')
                    try:
                        carrier.Shape = reorient(shape, settings['up'])
                        Mesh.export([carrier],
                                     os.path.join(output_dir, name))
                    finally:
                        doc.removeObject(carrier.Name)
                except Exception as exc:
                    errors.append({'source': index, 'body': obj.Name,
                                   'error': 'export failed: %s' % exc})
                    continue
                bodies.append({'source': index, 'name': obj.Name,
                               'label': obj.Label, 'settings': settings,
                               'brep': brep, 'output': name})
        finally:
            App.closeDocument(doc.Name)
    try:
        with open(results_path, 'w') as stream:
            stream.write(json.dumps({'bodies': bodies, 'errors': errors}))
    except Exception as exc:
        sys.stderr.write('fc-print: cannot write results: %s\n' % exc)
        sys.exit(1)


main()
