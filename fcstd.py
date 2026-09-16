"""Read, inspect, relink, and serialize FreeCAD .FCStd documents."""

from __future__ import annotations

import io
import os
import posixpath
import re
import zipfile
from typing import Literal

DocumentKind = Literal['part', 'assy', 'combined', 'unknown']

XLINK_TAG_RE: re.Pattern[str] = re.compile(r'<XLink\w*\b[^>]*>')
FILE_ATTR_RE: re.Pattern[str] = re.compile(r'file="([^"]*)"')
# FreeCAD saves the last recompute error message verbatim on the object tag
# (e.g. "Link broken! ... File: ../x.FCStd"). Informational only; FreeCAD
# regenerates it on recompute, so it is neither a reference nor rewritten.
OBJ_ERROR_ATTR_RE: re.Pattern[str] = re.compile(
    r'(?<=<Object )([^>]*?)Error="[^"]*"')
OBJ_TYPE_RE: re.Pattern[str] = re.compile(r'<Object type="([^"]+)"')
# Legal XML entities (the five predefined names plus numeric character
# references). A bare "&" anywhere else in an attribute value is not
# well-formed XML and is rejected at load time.
ENTITY_RE: re.Pattern[str] = re.compile(
    r'&(amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);')
BARE_AMP_RE: re.Pattern[str] = re.compile(
    r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9A-Fa-f]+);)')
GEOM_PREFIXES: tuple[str, ...] = ('PartDesign::', 'Part::', 'Sketcher::',
                                  'Mesh::')
ASSY_PREFIXES: tuple[str, ...] = ('Assembly::',)
ASSY_TYPES: tuple[str, ...] = ('App::Link',)
_ENTITY_NAMES: dict[str, str] = {'amp': '&', 'lt': '<', 'gt': '>',
                                 'quot': '"', 'apos': "'"}


class FCStdError(Exception):
    """Raised when an FCStd document cannot be loaded or serialized."""


# ----------------------------------------------------------------
# In-memory (no filesystem access) private helpers


def _unescape_entities(ref: str) -> str:
    """Decode legal XML entities in an XLink attribute value. No I/O."""

    def repl(m: re.Match[str]) -> str:
        name: str = m.group(1)
        if name.startswith('#x') or name.startswith('#X'):
            return chr(int(name[2:], 16))
        if name.startswith('#'):
            return chr(int(name[1:]))
        return _ENTITY_NAMES[name]

    return ENTITY_RE.sub(repl, ref)


def _escape_entities(ref: str) -> str:
    """Encode a path for a double-quoted XML attribute (incl. quotes). No I/O."""
    return (ref.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def _classify(xml: str, has_links: bool) -> DocumentKind:
    """Classify a document as ``part``/``assy``/``combined``/``unknown``.

    Preserves the original rules: a document with both geometry and assembly
    content is ``combined``; assembly-only is ``assy``; geometry-only is
    ``part``; otherwise ``unknown``. No I/O.
    """
    types: set[str] = set(OBJ_TYPE_RE.findall(xml))
    has_geom = any(t.startswith(GEOM_PREFIXES) for t in types)
    has_assy = (any(t.startswith(ASSY_PREFIXES) for t in types)
                or any(t in ASSY_TYPES for t in types)
                or has_links)
    if has_geom and has_assy:
        return 'combined'
    if has_assy:
        return 'assy'
    if has_geom:
        return 'part'
    return 'unknown'


def _blank_xlink_files(m: re.Match[str]) -> str:
    """Replace nonempty ``file=`` values inside one XLink tag with empty ones."""

    def blank(fm: re.Match[str]) -> str:
        if fm.group(1):
            return 'file=""'
        return fm.group(0)

    return FILE_ATTR_RE.sub(blank, m.group(0))


# ----------------------------------------------------------------
# Document model


class FCStdDocument:
    """An in-memory .FCStd document. Create instances with read()."""

    def __init__(self, source_path: str, members: dict[str, bytes], xml: str,
                 link_targets: list[str], kind: DocumentKind,
                 has_saved_error: bool) -> None:
        self._source_path = source_path
        self._members = members
        self._xml = xml
        # one normalized absolute target per nonempty XLink file attribute,
        # in document order (duplicates preserved)
        self._link_targets = link_targets
        self._kind = kind
        self._has_saved_error = has_saved_error

    @classmethod
    def read(cls, path: str) -> FCStdDocument:
        """Load and validate a document.

        Relative links are resolved from the document's absolute parent
        directory.

        Raises:
            FCStdError: If the archive cannot be read or contains unsupported
                or malformed content.
        """
        abspath = os.path.abspath(path)
        try:
            with zipfile.ZipFile(abspath) as z:
                names = z.namelist()
                if 'Document.xml' not in names:
                    raise FCStdError(f'{abspath}: no Document.xml inside; '
                                    f'not a FreeCAD document?')
                members = {name: z.read(name) for name in names}
        except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError,
                RuntimeError) as exc:
            raise FCStdError(f'{abspath}: cannot read archive ({exc})') from exc

        try:
            xml = members['Document.xml'].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FCStdError(f'{abspath}: Document.xml is not valid UTF-8') \
                from exc

        # Conservative .FCStd scan: mentions are allowed only inside XLink
        # file attributes and saved <Object Error> attributes. Mentions in
        # saved Error attributes only set has_saved_path_error.
        stripped = XLINK_TAG_RE.sub(_blank_xlink_files, xml)
        has_saved_error = any('.FCStd' in m.group(0)
                              for m in OBJ_ERROR_ATTR_RE.finditer(stripped))
        stripped = OBJ_ERROR_ATTR_RE.sub(r'\1Error=""', stripped)
        if '.FCStd' in stripped:
            raise FCStdError(f'{abspath}: Document.xml mentions .FCStd '
                            f'outside XLink file attributes; update this tool')
        for name, data in members.items():
            if name != 'Document.xml' and b'.FCStd' in data:
                raise FCStdError(f'{abspath}: zip member {name!r} contains '
                                f'".FCStd"; unknown schema usage, update '
                                f'this tool')

        doc_dir = os.path.dirname(abspath)
        link_targets: list[str] = []
        for tag in XLINK_TAG_RE.findall(xml):
            for raw in FILE_ATTR_RE.findall(tag):
                if not raw:
                    continue
                if BARE_AMP_RE.search(raw):
                    raise FCStdError(f'{abspath}: XLink path {raw!r} contains '
                                    f'an invalid XML entity; not well-formed '
                                    f'XML')
                ref = _unescape_entities(raw)
                if posixpath.isabs(ref) or (len(ref) > 1 and ref[1] == ':'):
                    raise FCStdError(f'{abspath}: absolute XLink path '
                                    f'{ref!r}; unsupported')
                link_targets.append(
                    os.path.normpath(os.path.join(doc_dir, ref)))

        return cls(abspath, members, xml, link_targets,
                   _classify(xml, bool(link_targets)), has_saved_error)

    def links(self) -> list[str]:
        """Return unique absolute link targets in first-occurrence order."""
        out: list[str] = []
        for target in self._link_targets:
            if target not in out:
                out.append(target)
        return out

    def relink(self, old: str, new: str) -> None:
        """Replace every matching link.

        Paths are normalized and made absolute, but are not checked for
        existence. No match is a no-op.
        """
        old_norm = os.path.normpath(os.path.abspath(old))
        new_norm = os.path.normpath(os.path.abspath(new))
        self._link_targets = [new_norm if t == old_norm else t
                              for t in self._link_targets]

    def classify(self) -> DocumentKind:
        """Return a heuristic classification intended as a display hint."""
        return self._kind

    @property
    def has_saved_path_error(self) -> bool:
        """Whether a saved FreeCAD object error mentions an FCStd path.

        Such recompute messages may be stale and are informational only.
        """
        return self._has_saved_error

    def serialize(self, path: str) -> bytes:
        """Serialize for the document's logical destination.

        Links are stored relative to the destination's directory. Unmodified
        XML and archive members are preserved.

        Raises:
            FCStdError: If a link cannot be made relative to the destination
                or the archive cannot be created.
        """
        abspath = os.path.abspath(path)
        doc_dir = os.path.dirname(abspath)
        rel_values: list[str] = []
        for target in self._link_targets:
            try:
                rel = os.path.relpath(target, doc_dir)
            except ValueError as exc:
                raise FCStdError(
                    f'{abspath}: cannot express link target {target!r} '
                    f'relative to {doc_dir!r} ({exc})') from exc
            rel_values.append(_escape_entities(rel.replace(os.sep, '/')))

        pending = iter(rel_values)

        def fix_file(fm: re.Match[str]) -> str:
            if not fm.group(1):
                return fm.group(0)
            try:
                new_value = next(pending)
            except StopIteration:
                raise FCStdError(f'{abspath}: internal error: XLink file '
                                f'occurrences changed since read') from None
            return f'file="{new_value}"'

        def fix_tag(m: re.Match[str]) -> str:
            return FILE_ATTR_RE.sub(fix_file, m.group(0))

        new_xml = XLINK_TAG_RE.sub(fix_tag, self._xml)

        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
                for name, data in self._members.items():
                    if name == 'Document.xml':
                        data = new_xml.encode('utf-8')
                    z.writestr(name, data)
        except (OSError, ValueError) as exc:
            raise FCStdError(f'{abspath}: cannot serialize document ({exc})') \
                from exc
        return buf.getvalue()
