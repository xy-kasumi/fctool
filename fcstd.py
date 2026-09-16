"""Pure FreeCAD .FCStd archive/XML knowledge, isolated from the CLI.

An .FCStd file is a ZIP archive whose FreeCAD document tree lives in a
`Document.xml` member. Cross-document references appear as `file="..."`
attributes inside `<XLink*>` tags; those values are slash-separated paths
relative to the referring document's directory.

This module is split into two explicitly labelled sections:

* **In-memory (no filesystem access)** helpers operate only on strings,
  member mappings (`dict[str, bytes]`), or path strings. They never touch
  the filesystem, never print, and never call `sys.exit`.
* **Filesystem boundary** helpers are the *only* functions here that can
  read from or write to disk. All I/O is confined to these functions.

All FCStd-member content is held in memory as `dict[str, bytes]`; nothing
here lazily reopens a source archive after reading it.
"""

import os
import posixpath
import re
import zipfile

XLINK_TAG_RE = re.compile(r'<XLink\w*\b[^>]*>')
FILE_ATTR_RE = re.compile(r'file="([^"]*)"')
# FreeCAD saves the last recompute error message verbatim on the object tag
# (e.g. "Link broken! ... File: ../x.FCStd"). Informational only; FreeCAD
# regenerates it on recompute, so it is neither a reference nor rewritten.
OBJ_ERROR_ATTR_RE = re.compile(r'(?<=<Object )([^>]*?)Error="[^"]*"')
OBJ_TYPE_RE = re.compile(r'<Object type="([^"]+)"')
GEOM_PREFIXES = ('PartDesign::', 'Part::', 'Sketcher::', 'Mesh::')
ASSY_PREFIXES = ('Assembly::',)
ASSY_TYPES = ('App::Link',)


class FCStdError(Exception):
    """Unsupported or malformed FCStd content.

    Raised by the pure helpers here to report a condition the CLI cannot
    safely handle. The CLI translates these into the existing diagnostic
    wording; this module itself never prints or exits.
    """


# ----------------------------------------------------------------
# In-memory (no filesystem access)


def document_xml(members):
    """Return the UTF-8 text of the ``Document.xml`` member (no I/O)."""
    return members['Document.xml'].decode('utf-8')


def xlink_refs(xml):
    """Return unique nonempty ``file="..."`` values inside ``<XLink*>`` tags.

    Order is first-seen and preserved. XML-escaped paths (values containing
    ``&``, ``<`` or ``>``) are rejected with :class:`FCStdError` because they
    cannot be safely rewritten. No I/O.
    """
    refs = []
    for tag in XLINK_TAG_RE.findall(xml):
        for val in FILE_ATTR_RE.findall(tag):
            if val and val not in refs:
                if re.search(r'[&<>]', val):
                    raise FCStdError(
                        f'XLink path {val!r} contains XML-escaped characters; '
                        f'unsupported, update this tool')
                refs.append(val)
    return refs


def classify(xml):
    """Classify a document as ``part``/``assy``/``combined``/``unknown``.

    Preserves the original rules: a document with both geometry and assembly
    content is ``combined``; assembly-only is ``assy``; geometry-only is
    ``part``; otherwise ``unknown``. No I/O.
    """
    types = set(OBJ_TYPE_RE.findall(xml))
    has_geom = any(t.startswith(GEOM_PREFIXES) for t in types)
    has_assy = (any(t.startswith(ASSY_PREFIXES) for t in types)
                or any(t in ASSY_TYPES for t in types)
                or bool(xlink_refs(xml)))
    if has_geom and has_assy:
        return 'combined'
    if has_assy:
        return 'assy'
    if has_geom:
        return 'part'
    return 'unknown'


def resolve_ref(doc_path, ref):
    """Resolve an XLink ``file=`` path against its referring document.

    XLink paths are relative to the referring document's directory. Absolute
    and drive-letter paths are rejected. This is path arithmetic only -- no
    existence check. No I/O.
    """
    if posixpath.isabs(ref) or (len(ref) > 1 and ref[1] == ':'):
        raise FCStdError(
            f'{doc_path}: absolute XLink path {ref!r}; unsupported')
    return os.path.normpath(os.path.join(os.path.dirname(doc_path), ref))


def rewrite_xlinks(xml, rewrite_ref):
    """Apply ``rewrite_ref`` to each nonempty ``file=`` attribute.

    ``rewrite_ref`` receives the current attribute value and must return the
    replacement value. It is applied only to nonempty ``file`` attributes
    inside ``<XLink*>`` tags; all other XML text is preserved verbatim.
    Replacement values requiring XML escaping (``&``, ``<``, ``>`` or ``"``)
    are rejected with :class:`FCStdError`. No I/O.
    """

    def _fix_file(fm):
        old = fm.group(1)
        if not old:
            return fm.group(0)
        new = rewrite_ref(old)
        if re.search(r'[&<>"]', new):
            raise FCStdError(f'new path {new!r} needs XML escaping; unsupported')
        return f'file="{new}"'

    def _fix_tag(m):
        return FILE_ATTR_RE.sub(_fix_file, m.group(0))

    return XLINK_TAG_RE.sub(_fix_tag, xml)


def validate_supported_references(members):
    """Scan a member mapping for ``.FCStd`` byte sequences we don't understand.

    Within ``Document.xml``, any remaining ``.FCStd`` mention outside XLink
    ``file`` attributes (and outside saved recompute-``Error`` attributes,
    which are informational and left untouched) raises :class:`FCStdError`.
    A ``.FCStd`` mention in any other ZIP member likewise raises.

    Returns ``True`` when a saved Object ``Error`` attribute mentions
    ``.FCStd`` (so the caller can emit its existing warning) and ``False``
    otherwise. No I/O.
    """
    has_saved_error = False
    for name, data in members.items():
        if name == 'Document.xml':
            xml = data.decode('utf-8')
            stripped = XLINK_TAG_RE.sub(
                lambda m: FILE_ATTR_RE.sub('file=""', m.group(0)), xml)
            errors = [m.group(0) for m in OBJ_ERROR_ATTR_RE.finditer(stripped)]
            if any('.FCStd' in e for e in errors):
                has_saved_error = True
            stripped = OBJ_ERROR_ATTR_RE.sub(r'\1Error=""', stripped)
            if '.FCStd' in stripped:
                raise FCStdError(
                    f'Document.xml mentions .FCStd outside XLink file '
                    f'attributes; update this tool')
        elif b'.FCStd' in data:
            raise FCStdError(
                f'zip member {name!r} contains ".FCStd"; unknown schema '
                f'usage, update this tool')
    return has_saved_error


# ----------------------------------------------------------------
# Filesystem boundary


def read_fcstd(path):
    """Read all ZIP members of ``path`` into ``{name: bytes}``.

    Rejects an archive lacking ``Document.xml``, retaining the path in the
    error. This is the only function that opens archives for reading.
    """
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if 'Document.xml' not in names:
            raise FCStdError(f'{path}: no Document.xml inside; '
                             f'not a FreeCAD document?')
        return {n: z.read(n) for n in names}


def write_fcstd(path, members):
    """Write ``members`` to ``path`` atomically.

    Writes to ``path + '.fc-assy.tmp'`` with deflated compression and then
    ``os.replace``-s into place. Member contents and order are preserved.
    This is the only function that writes archives.
    """
    tmp = path + '.fc-assy.tmp'
    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    os.replace(tmp, path)
