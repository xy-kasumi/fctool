"""Read, inspect, relink, and serialize FreeCAD .FCStd documents."""

from __future__ import annotations

import io
import os
import posixpath
import zipfile
from typing import Literal, cast
from xml.dom import minidom
from xml.dom.minidom import Document, Element
from xml.parsers.expat import ExpatError

DocumentKind = Literal['part', 'assy', 'combined', 'unknown']

GEOM_PREFIXES: tuple[str, ...] = ('PartDesign::', 'Part::', 'Sketcher::',
                                  'Mesh::')
ASSY_PREFIXES: tuple[str, ...] = ('Assembly::',)
ASSY_TYPES: tuple[str, ...] = ('App::Link',)


class FCStdError(Exception):
    """Raised when an FCStd document cannot be loaded or serialized."""


# ----------------------------------------------------------------
# XML helpers


def _element_name(element: Element) -> str:
    """Return an element's local name, including for namespace-aware XML."""
    return element.localName or element.tagName


def _is_xlink(element: Element) -> bool:
    return _element_name(element).startswith('XLink')


def _validate_references(document: Document, path: str) -> bool:
    """Reject FCStd mentions outside XLink paths and saved errors."""
    scrubbed = cast(Document, document.cloneNode(deep=True))
    has_saved_error = False
    for element in scrubbed.getElementsByTagName('*'):
        if _is_xlink(element) and element.hasAttribute('file'):
            element.setAttribute('file', '')
        if (_element_name(element) == 'Object'
                and element.hasAttribute('Error')):
            has_saved_error |= '.FCStd' in element.getAttribute('Error')
            element.setAttribute('Error', '')

    if '.FCStd' in scrubbed.toxml():
        raise FCStdError(f'{path}: Document.xml mentions .FCStd outside '
                         f'XLink file attributes; update this tool')
    return has_saved_error


def _classify(document: Document, has_links: bool) -> DocumentKind:
    types = {
        element.getAttribute('type')
        for element in document.getElementsByTagName('*')
        if _element_name(element) == 'Object' and element.hasAttribute('type')
    }
    has_geom = any(value.startswith(GEOM_PREFIXES) for value in types)
    has_assy = (any(value.startswith(ASSY_PREFIXES) for value in types)
                or any(value in ASSY_TYPES for value in types)
                or has_links)
    if has_geom and has_assy:
        return 'combined'
    if has_assy:
        return 'assy'
    if has_geom:
        return 'part'
    return 'unknown'


# ----------------------------------------------------------------
# Document model


class FCStdDocument:
    """An in-memory .FCStd document. Create instances with read()."""

    def __init__(self, members: dict[str, bytes], document: Document,
                 links: list[tuple[Element, str, str]], kind: DocumentKind,
                 has_saved_error: bool) -> None:
        self._members = members
        self._document = document
        # Each entry is (XML element, original relative ref, absolute target).
        self._links = links
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
            with zipfile.ZipFile(abspath) as archive:
                names = archive.namelist()
                if 'Document.xml' not in names:
                    raise FCStdError(f'{abspath}: no Document.xml inside; '
                                     f'not a FreeCAD document?')
                members = {name: archive.read(name) for name in names}
        except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError,
                RuntimeError) as exc:
            raise FCStdError(f'{abspath}: cannot read archive ({exc})') from exc

        xml_bytes = members['Document.xml']
        try:
            xml = xml_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FCStdError(f'{abspath}: Document.xml is not valid UTF-8') \
                from exc
        try:
            document = minidom.parseString(xml)
        except (ExpatError, ValueError) as exc:
            raise FCStdError(f'{abspath}: Document.xml is not well-formed XML '
                             f'({exc})') from exc

        has_saved_error = _validate_references(document, abspath)
        for name, data in members.items():
            if name != 'Document.xml' and b'.FCStd' in data:
                raise FCStdError(f'{abspath}: zip member {name!r} contains '
                                 f'".FCStd"; unknown schema usage, update '
                                 f'this tool')

        links: list[tuple[Element, str, str]] = []
        doc_dir = os.path.dirname(abspath)
        for element in document.getElementsByTagName('*'):
            if not _is_xlink(element) or not element.hasAttribute('file'):
                continue
            ref = element.getAttribute('file')
            if not ref:
                continue
            if posixpath.isabs(ref) or (len(ref) > 1 and ref[1] == ':'):
                raise FCStdError(f'{abspath}: absolute XLink path {ref!r}; '
                                 f'unsupported')
            target = os.path.normpath(os.path.join(doc_dir, ref))
            links.append((element, ref, target))

        return cls(members, document, links,
                   _classify(document, bool(links)), has_saved_error)

    def links(self) -> list[str]:
        """Return unique absolute link targets in first-occurrence order."""
        return list(dict.fromkeys(target for _, _, target in self._links))

    def relink(self, old: str, new: str) -> None:
        """Replace every matching link.

        Paths are normalized and made absolute, but are not checked for
        existence. No match is a no-op.
        """
        old_norm = os.path.normpath(os.path.abspath(old))
        new_norm = os.path.normpath(os.path.abspath(new))
        self._links = [
            (element, ref, new_norm if target == old_norm else target)
            for element, ref, target in self._links
        ]

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

        Links are stored relative to the destination's directory. Other ZIP
        members are preserved byte-for-byte. Document.xml is also preserved
        byte-for-byte when no link needs changing; otherwise the standard XML
        serializer rewrites it without changing its XML meaning.

        Raises:
            FCStdError: If a link cannot be made relative to the destination
                or the archive cannot be created.
        """
        abspath = os.path.abspath(path)
        doc_dir = os.path.dirname(abspath)
        refs: list[str] = []
        for _, _, target in self._links:
            try:
                ref = os.path.relpath(target, doc_dir)
            except ValueError as exc:
                raise FCStdError(
                    f'{abspath}: cannot express link target {target!r} '
                    f'relative to {doc_dir!r} ({exc})') from exc
            refs.append(ref.replace(os.sep, '/'))

        if refs == [original for _, original, _ in self._links]:
            xml_bytes = self._members['Document.xml']
        else:
            for (element, _, _), ref in zip(self._links, refs, strict=True):
                element.setAttribute('file', ref)
            xml_bytes = self._document.toxml(encoding='utf-8')

        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in self._members.items():
                    archive.writestr(
                        name, xml_bytes if name == 'Document.xml' else data)
        except (OSError, ValueError) as exc:
            raise FCStdError(f'{abspath}: cannot serialize document ({exc})') \
                from exc
        return buf.getvalue()
