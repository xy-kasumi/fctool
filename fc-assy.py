#!/usr/bin/env python3
"""fc-assy: inspect and safely move/rename FreeCAD .FCStd files.

Commands:
  fc-assy.py show <paths...>          classify files and list assembly deps
  fc-assy.py rshow [--root DIR] <paths...>
                                      classify files and list reverse assembly
                                      deps in the tree rooted at cwd
                                      (override with --root)
  fc-assy.py mv <srcs...> <dst>       move/rename files, updating all XLink
                                      references in the tree rooted at cwd
                                      (override with --root)

mv semantics: a FILE source behaves like mv (into dst if dst is an
existing dir, else rename). A DIRECTORY source always maps its contents
(src/f -> dst/f) whether or not dst exists -- it never nests as
dst/src/f. To nest, name the destination explicitly: mv src dst/src

mv is all-or-nothing: every validation runs against an in-memory result
first; the filesystem is only touched after everything checks out.
The tool operates with partial knowledge of the FCStd schema, so any
".FCStd" byte sequence it does not positively understand aborts the
operation (update this tool rather than risk silent breakage).
"""

import argparse
import os
import posixpath
import re
import sys
import time
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


def fail(msg):
    print(f'fc-assy: error: {msg}', file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f'fc-assy: warning: {msg}', file=sys.stderr)


def find_fcstd(root):
    out = []
    t0 = time.monotonic()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != '.git']
        if not find_fcstd.warned and time.monotonic() - t0 > 5:
            warn(f'still scanning {root} for .FCStd files; '
                 f'if this tree is mostly unrelated, pass --root to narrow it')
            find_fcstd.warned = True
        for f in filenames:
            if f.endswith('.FCStd'):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


find_fcstd.warned = False


def load_doc(path):
    """Returns {member_name: bytes}. Fails on docs without Document.xml."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if 'Document.xml' not in names:
            fail(f'{path}: no Document.xml inside; not a FreeCAD document?')
        return {n: z.read(n) for n in names}


def doc_xml(members):
    return members['Document.xml'].decode('utf-8')


def xlink_refs(xml):
    """Unique nonempty file="..." values inside <XLink*> tags, in order."""
    refs = []
    for tag in XLINK_TAG_RE.findall(xml):
        for val in FILE_ATTR_RE.findall(tag):
            if val and val not in refs:
                if re.search(r'[&<>]', val):
                    fail(f'XLink path {val!r} contains XML-escaped characters; '
                         f'unsupported, update this tool')
                refs.append(val)
    return refs


def classify(xml):
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
    """XLink file= paths are relative to the referring document's dir."""
    if posixpath.isabs(ref) or (len(ref) > 1 and ref[1] == ':'):
        fail(f'{doc_path}: absolute XLink path {ref!r}; unsupported')
    return os.path.normpath(os.path.join(os.path.dirname(doc_path), ref))


# ---------------------------------------------------------------- show

def cmd_show(paths):
    for p in paths:
        if not p.endswith('.FCStd') or not os.path.isfile(p):
            warn(f'{p}: not an existing .FCStd file, ignored')
            continue
        show_tree(os.path.abspath(p), depth=0, seen=set())


# ---------------------------------------------------------------- rshow

def cmd_rshow(paths, root):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        fail(f'--root {root}: not a directory')

    # Filter input paths exactly like cmd_show.
    requested = []
    for p in paths:
        if not p.endswith('.FCStd') or not os.path.isfile(p):
            warn(f'{p}: not an existing .FCStd file, ignored')
            continue
        requested.append(os.path.abspath(p))

    # Build a reverse XLink index: resolved absolute target -> set of
    # absolute referring documents. Only referring docs inside the scan root
    # participate; the requested file itself need not be under it.
    referrers = {}
    xml_cache = {}
    for p in find_fcstd(root):
        abspath = os.path.abspath(p)
        members = load_doc(abspath)
        xml_cache[abspath] = doc_xml(members)
        for ref in xlink_refs(xml_cache[abspath]):
            target = resolve_ref(abspath, ref)
            referrers.setdefault(target, set()).add(abspath)

    # Load valid requested files outside the scan root so they can still be
    # classified (their XML is not part of the scan, so cache it separately).
    for abspath in requested:
        if abspath not in xml_cache:
            xml_cache[abspath] = doc_xml(load_doc(abspath))

    for abspath in requested:
        rshow_tree(abspath, depth=0, seen=set(), referrers=referrers,
                   xml_cache=xml_cache)


def rshow_tree(abspath, depth, seen, referrers, xml_cache):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    xml = xml_cache[abspath]
    kind = classify(xml)
    note = ''
    if kind == 'combined':
        note = '  (combined part+assy: NOT SUPPORTED by this tool)'
    if abspath in seen:
        print(f'{indent}{rel}  [{kind}] (already shown)')
        return
    print(f'{indent}{rel}  [{kind}]{note}')
    seen.add(abspath)
    for t in sorted(referrers.get(abspath, ())):
        rshow_tree(t, depth + 1, seen, referrers, xml_cache)


def show_tree(abspath, depth, seen):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    if not os.path.isfile(abspath):
        print(f'{indent}{rel}  [MISSING]')
        return
    xml = doc_xml(load_doc(abspath))
    kind = classify(xml)
    note = ''
    if kind == 'combined':
        note = '  (combined part+assy: NOT SUPPORTED by this tool)'
    if abspath in seen:
        print(f'{indent}{rel}  [{kind}] (already shown)')
        return
    print(f'{indent}{rel}  [{kind}]{note}')
    seen.add(abspath)
    if kind in ('assy', 'combined'):
        targets = [resolve_ref(abspath, r) for r in xlink_refs(xml)]
        for t in sorted(targets):
            show_tree(t, depth + 1, seen)


# ---------------------------------------------------------------- mv

def expand_moves(srcs, dst):
    """mv-like semantics -> {abs_src_file: abs_dst_file} plus non-FCStd list."""
    dst = os.path.abspath(dst)
    moves, plain = {}, {}

    def add(src_file, dst_file):
        if not src_file.endswith('.FCStd'):
            plain[src_file] = dst_file
        else:
            if not dst_file.endswith('.FCStd'):
                fail(f'refusing to rename {src_file} to non-.FCStd {dst_file}')
            moves[src_file] = dst_file

    dst_is_dir = os.path.isdir(dst)
    if len(srcs) > 1 and not dst_is_dir:
        fail(f'target {dst!r} must be an existing directory '
             f'when moving multiple sources')
    for s in srcs:
        s = os.path.abspath(s)
        if os.path.isfile(s):
            add(s, os.path.join(dst, os.path.basename(s)) if dst_is_dir else dst)
        elif os.path.isdir(s):
            # A directory source always maps its CONTENTS into dst
            # (src/f -> dst/f), independent of whether dst exists.
            # Unlike mv, never nests as dst/basename(src); to nest,
            # name the destination explicitly: mv src dst/src
            for dirpath, dirnames, filenames in os.walk(s):
                dirnames[:] = [d for d in dirnames if d != '.git']
                for f in filenames:
                    sf = os.path.join(dirpath, f)
                    add(sf, os.path.join(dst, os.path.relpath(sf, s)))
        else:
            fail(f'{s}: no such file or directory')
    return moves, plain


def cmd_mv(srcs, dst, root):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        fail(f'--root {root}: not a directory')
    moves, plain_moves = expand_moves(srcs, dst)
    all_moves = {**moves, **plain_moves}
    if not all_moves:
        fail('nothing to move')

    # -- validate the move map itself; sources must be inside the tree too,
    #    or their own outgoing links would escape rewriting
    for src, d in all_moves.items():
        if not os.path.commonpath([src, root]) == root:
            fail(f'source {src} is outside the tree rooted at {root}')
        if not os.path.commonpath([d, root]) == root:
            fail(f'destination {d} is outside the tree rooted at {root}')
        if src == d:
            fail(f'{src}: source and destination are identical')
    dsts = list(all_moves.values())
    if len(set(dsts)) != len(dsts):
        fail('two sources map to the same destination')
    for d in dsts:
        if os.path.exists(d) and d not in all_moves:
            fail(f'destination {os.path.relpath(d)} already exists')

    # -- load every document in the tree
    docs = {}  # abs path -> members dict
    for p in find_fcstd(root):
        docs[os.path.abspath(p)] = load_doc(p)

    final_path = {p: moves.get(p, p) for p in docs}

    # -- refuse unsupported document structures among involved docs
    involved = set(moves)
    for p, members in docs.items():
        refs = xlink_refs(doc_xml(members))
        if any(resolve_ref(p, r) in moves for r in refs):
            involved.add(p)
    for p in involved:
        kind = classify(doc_xml(docs[p]))
        if kind not in ('part', 'assy'):
            fail(f'{os.path.relpath(p)}: classified as {kind!r}; '
                 f'refusing to touch it')

    # -- compute rewritten Document.xml for every doc (in memory)
    changed = {}  # abs old path -> new members dict
    for p, members in docs.items():
        xml = doc_xml(members)
        new_dir = os.path.dirname(final_path[p])

        def fix_tag(m):
            tag = m.group(0)

            def fix_file(fm):
                old = fm.group(1)
                if not old:
                    return fm.group(0)
                target = resolve_ref(p, old)
                if not os.path.isfile(target):
                    fail(f'{os.path.relpath(p)}: pre-existing broken XLink '
                         f'to {old!r}; fix that first')
                new_rel = os.path.relpath(final_path.get(target, target), new_dir)
                new_rel = new_rel.replace(os.sep, '/')
                if re.search(r'[&<>"]', new_rel):
                    fail(f'new path {new_rel!r} needs XML escaping; unsupported')
                return f'file="{new_rel}"'

            return FILE_ATTR_RE.sub(fix_file, tag)

        new_xml = XLINK_TAG_RE.sub(fix_tag, xml)
        if new_xml != xml:
            changed[p] = {**members, 'Document.xml': new_xml.encode('utf-8')}

    # -- paranoia scan of the FINAL state: any ".FCStd" byte sequence we do
    #    not positively understand aborts the whole operation.
    for p, members in docs.items():
        members = changed.get(p, members)
        for name, data in members.items():
            if name == 'Document.xml':
                xml = data.decode('utf-8')
                stripped = XLINK_TAG_RE.sub(
                    lambda m: FILE_ATTR_RE.sub('file=""', m.group(0)),
                    xml)
                errors = [m.group(0) for m in OBJ_ERROR_ATTR_RE.finditer(stripped)]
                if any('.FCStd' in e for e in errors):
                    warn(f'{os.path.relpath(p)}: has saved recompute errors '
                         f'mentioning .FCStd (stale "Link broken!" messages?); '
                         f'left as-is, recompute and save in FreeCAD to clear')
                stripped = OBJ_ERROR_ATTR_RE.sub(r'\1Error=""', stripped)
                if '.FCStd' in stripped:
                    fail(f'{os.path.relpath(p)}: Document.xml mentions .FCStd '
                         f'outside XLink file attributes; update this tool')
            elif b'.FCStd' in data:
                fail(f'{os.path.relpath(p)}: zip member {name!r} contains '
                     f'".FCStd"; unknown schema usage, update this tool')

    # -- final in-memory resolution check against the simulated final layout
    final_files = set(final_path.values()) | set(plain_moves.values())
    for p, members in docs.items():
        members = changed.get(p, members)
        for ref in xlink_refs(doc_xml(members)):
            target = resolve_ref(final_path[p], ref)
            existing_unmoved = target not in all_moves and os.path.isfile(target)
            if target not in final_files and not existing_unmoved:
                fail(f'{os.path.relpath(final_path[p])}: link {ref!r} would '
                     f'not resolve after the move')

    # -- everything validated: apply to filesystem
    def write_zip(path, members):
        tmp = path + '.fc-assy.tmp'
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as z:
            for name, data in members.items():
                z.writestr(name, data)
        os.replace(tmp, path)

    n_rewritten = 0
    for src, d in sorted(all_moves.items()):
        os.makedirs(os.path.dirname(d), exist_ok=True)
    for p, members in changed.items():
        write_zip(p, members)  # rewrite in place first (src still exists)
        n_rewritten += 1
    # two-phase move so overlapping src/dst sets (e.g. b->c with a->b) never clobber
    staged = {}
    for i, (src, d) in enumerate(sorted(all_moves.items())):
        tmp = os.path.join(os.path.dirname(src), f'.fc-assy.stage.{i}')
        os.replace(src, tmp)
        staged[tmp] = d
    for tmp, d in staged.items():
        os.replace(tmp, d)
    for src in sorted(all_moves, reverse=True):  # prune emptied dirs
        sd = os.path.dirname(src)
        while sd != root:
            try:
                os.rmdir(sd)
            except OSError:
                break
            sd = os.path.dirname(sd)

    # -- verify from disk
    for p in find_fcstd(root):
        members = load_doc(p)
        for ref in xlink_refs(doc_xml(members)):
            target = resolve_ref(os.path.abspath(p), ref)
            if not os.path.isfile(target):
                fail(f'POST-CHECK FAILED: {p}: {ref!r} does not resolve. '
                     f'Restore from version control and report this.')

    print(f'moved {len(all_moves)} file(s), '
          f'rewrote links in {n_rewritten} document(s); all links verified')


def main():
    ap = argparse.ArgumentParser(prog='fc-assy', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    ps = sub.add_parser('show', help='classify files and list assembly deps')
    ps.add_argument('paths', nargs='+')
    pr = sub.add_parser('rshow',
                        help='classify files and list reverse assembly deps')
    pr.add_argument('--root', default='.',
                    help='tree scanned for referring documents; references '
                         'from outside it are not shown (default: cwd)')
    pr.add_argument('paths', nargs='+')
    pm = sub.add_parser('mv', help='move/rename files, updating references')
    pm.add_argument('--root', default='.',
                    help='tree to scan for referring documents and to keep '
                         'consistent (default: cwd); links from outside are '
                         'the caller\'s responsibility')
    pm.add_argument('paths', nargs='+', metavar='SRC... DST')
    args = ap.parse_args()
    if args.cmd == 'show':
        cmd_show(args.paths)
    elif args.cmd == 'rshow':
        cmd_rshow(args.paths, args.root)
    else:
        if len(args.paths) < 2:
            fail('mv needs at least one source and a destination')
        cmd_mv(args.paths[:-1], args.paths[-1], args.root)


if __name__ == '__main__':
    main()
