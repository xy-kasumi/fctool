"""Read, inspect, relink, and serialize FreeCAD .FCStd documents."""

from __future__ import annotations

import io
import os
import posixpath
import zipfile
from typing import Literal
from xml.etree import ElementTree as ET
from xml.etree.ElementTree import Element

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
    tag = element.tag
    if not isinstance(tag, str):  # comments and processing instructions
        return ''
    return tag.rsplit('}', 1)[-1].split(':', 1)[-1]


def _is_xlink(element: Element) -> bool:
    return _element_name(element).startswith('XLink')


def _unsupported_reference(path: str) -> FCStdError:
    return FCStdError(f'{path}: Document.xml mentions .FCStd outside '
                      f'XLink file attributes; update this tool')


class _TreeBuilder(ET.TreeBuilder):
    """Preserve XML nodes and reject references outside the element tree.

    ElementTree omits comments/PIs outside the root and namespace declaration
    attributes from the returned tree. Check those parser events here; the
    ordinary elements, attributes, and text are checked after parsing.
    """

    def __init__(self, path: str) -> None:
        super().__init__(insert_comments=True, insert_pis=True)
        self.path = path

    def comment(self, text: str) -> Element | None:
        if '.FCStd' in text:
            raise _unsupported_reference(self.path)
        return super().comment(text)

    def pi(self, target: str, text: str | None = None) -> Element | None:
        if '.FCStd' in target or (text is not None and '.FCStd' in text):
            raise _unsupported_reference(self.path)
        return super().pi(target, text)

    def start_ns(self, prefix: str, uri: str) -> None:
        if '.FCStd' in prefix or '.FCStd' in uri:
            raise _unsupported_reference(self.path)

    def doctype(self, name: str, pubid: str | None,
                system: str | None) -> None:
        if any(value is not None and '.FCStd' in value
               for value in (name, pubid, system)):
            raise _unsupported_reference(self.path)


def _validate_references(document: Element, path: str) -> bool:
    """Reject FCStd mentions outside XLink paths and saved errors."""
    has_saved_error = False
    for element in document.iter():
        name = _element_name(element)
        if isinstance(element.tag, str) and '.FCStd' in element.tag:
            raise _unsupported_reference(path)
        if ((element.text is not None and '.FCStd' in element.text)
                or (element.tail is not None and '.FCStd' in element.tail)):
            raise _unsupported_reference(path)
        for attr, value in element.attrib.items():
            allowed_link = _is_xlink(element) and attr == 'file'
            allowed_error = name == 'Object' and attr == 'Error'
            if '.FCStd' in attr or ('.FCStd' in value
                                    and not allowed_link
                                    and not allowed_error):
                raise _unsupported_reference(path)
            if allowed_error:
                has_saved_error |= '.FCStd' in value
    return has_saved_error


def _classify(document: Element, has_links: bool) -> DocumentKind:
    types = {
        element.get('type', '')
        for element in document.iter()
        if _element_name(element) == 'Object' and 'type' in element.attrib
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

    def __init__(self, members: dict[str, bytes], document: Element,
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
            parser = ET.XMLParser(target=_TreeBuilder(abspath))
            document = ET.fromstring(xml, parser=parser)
        except (ET.ParseError, ValueError) as exc:
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
        for element in document.iter():
            if not _is_xlink(element) or 'file' not in element.attrib:
                continue
            ref = element.get('file', '')
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
                element.set('file', ref)
            # Match minidom's compact lexical style. ElementTree otherwise
            # inserts a newline after a single-quoted declaration and spaces
            # before ``/>``; neither is useful in FreeCAD's Document.xml and
            # both would make unrelated content appear changed.
            body = ET.tostring(self._document, encoding='utf-8',
                               short_empty_elements=True)
            xml_bytes = (b'<?xml version="1.0" encoding="utf-8"?>'
                         + body.replace(b' />', b'/>'))

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
