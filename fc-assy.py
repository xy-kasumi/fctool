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
import sys
import time

import fcstd


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


# ---------------------------------------------------------------- show

def cmd_show(paths):
    for p in paths:
        if not p.endswith('.FCStd') or not os.path.isfile(p):  # input I/O
            warn(f'{p}: not an existing .FCStd file, ignored')
            continue
        show_tree(os.path.abspath(p), depth=0, seen=set())


def show_tree(abspath, depth, seen):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    if not os.path.isfile(abspath):  # existence check (command-side I/O)
        print(f'{indent}{rel}  [MISSING]')
        return
    # read from disk, then classify/reference-extract in memory
    members = fcstd.read_fcstd(abspath)
    xml = fcstd.document_xml(members)
    kind = fcstd.classify(xml)
    note = ''
    if kind == 'combined':
        note = '  (combined part+assy: NOT SUPPORTED by this tool)'
    if abspath in seen:
        print(f'{indent}{rel}  [{kind}] (already shown)')
        return
    print(f'{indent}{rel}  [{kind}]{note}')
    seen.add(abspath)
    if kind in ('assy', 'combined'):
        targets = [fcstd.resolve_ref(abspath, r) for r in fcstd.xlink_refs(xml)]
        for t in sorted(targets):
            show_tree(t, depth + 1, seen)


# ---------------------------------------------------------------- rshow

def cmd_rshow(paths, root):
    root = os.path.abspath(root)
    if not os.path.isdir(root):  # input I/O
        fail(f'--root {root}: not a directory')

    # Filter input paths exactly like cmd_show. (existence check: command I/O)
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
    for p in find_fcstd(root):  # scan/read: command I/O
        abspath = os.path.abspath(p)
        members = fcstd.read_fcstd(abspath)
        xml_cache[abspath] = fcstd.document_xml(members)
        for ref in fcstd.xlink_refs(xml_cache[abspath]):  # in-memory
            target = fcstd.resolve_ref(abspath, ref)
            referrers.setdefault(target, set()).add(abspath)

    # Load valid requested files outside the scan root so they can still be
    # classified (their XML is not part of the scan, so cache it separately).
    for abspath in requested:
        if abspath not in xml_cache:
            xml_cache[abspath] = fcstd.document_xml(fcstd.read_fcstd(abspath))

    for abspath in requested:
        rshow_tree(abspath, depth=0, seen=set(), referrers=referrers,
                   xml_cache=xml_cache)


def rshow_tree(abspath, depth, seen, referrers, xml_cache):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    xml = xml_cache[abspath]
    kind = fcstd.classify(xml)
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

    dst_is_dir = os.path.isdir(dst)  # existence check: command I/O
    if len(srcs) > 1 and not dst_is_dir:
        fail(f'target {dst!r} must be an existing directory '
             f'when moving multiple sources')
    for s in srcs:
        s = os.path.abspath(s)
        if os.path.isfile(s):  # existence check: command I/O
            add(s, os.path.join(dst, os.path.basename(s)) if dst_is_dir else dst)
        elif os.path.isdir(s):  # existence check: command I/O
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
    # -- Phase 1: filesystem discovery / snapshot
    root = os.path.abspath(root)
    if not os.path.isdir(root):  # existence check: command I/O
        fail(f'--root {root}: not a directory')
    moves, plain_moves = expand_moves(srcs, dst)
    all_moves = {**moves, **plain_moves}
    if not all_moves:
        fail('nothing to move')

    # validate the move map itself; sources must be inside the tree too,
    # or their own outgoing links would escape rewriting
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
        if os.path.exists(d) and d not in all_moves:  # existence check: cmd I/O
            fail(f'destination {os.path.relpath(d)} already exists')

    # load every document once, cache XML and refs
    docs = {}      # abs path -> members dict
    xmls = {}      # abs path -> Document.xml text
    refs = {}      # abs path -> list[str] xlink refs
    for p in find_fcstd(root):  # scan/read: command I/O
        abspath = os.path.abspath(p)
        members = fcstd.read_fcstd(abspath)
        docs[abspath] = members
        xmls[abspath] = fcstd.document_xml(members)
        refs[abspath] = fcstd.xlink_refs(xmls[abspath])

    final_path = {p: moves.get(p, p) for p in docs}

    # Perform every pre-move existence check for resolved link targets once,
    # retaining the results so later phases make no isfile calls.
    target_exists = {}  # abs resolved target -> bool
    for p in docs:
        for r in refs[p]:
            target = fcstd.resolve_ref(p, r)
            target_exists.setdefault(target, os.path.isfile(target))

    # -- Phase 2: in-memory plan / validation (no filesystem access)
    # refuse unsupported document structures among involved docs
    involved = set(moves)
    for p in docs:
        if any(fcstd.resolve_ref(p, r) in moves for r in refs[p]):
            involved.add(p)
    for p in involved:
        kind = fcstd.classify(xmls[p])
        if kind not in ('part', 'assy'):
            fail(f'{os.path.relpath(p)}: classified as {kind!r}; '
                 f'refusing to touch it')

    # compute rewritten Document.xml for every doc (in memory)
    changed = {}  # abs old path -> new members dict
    for p, members in docs.items():
        new_dir = os.path.dirname(final_path[p])

        def make_rewrite_ref(doc):
            def rewrite_ref(old):
                target = fcstd.resolve_ref(doc, old)
                if not target_exists[target]:
                    fail(f'{os.path.relpath(doc)}: pre-existing broken XLink '
                         f'to {old!r}; fix that first')
                new_rel = os.path.relpath(final_path.get(target, target),
                                          new_dir)
                return new_rel.replace(os.sep, '/')
            return rewrite_ref

        new_xml = fcstd.rewrite_xlinks(xmls[p], make_rewrite_ref(p))
        if new_xml != xmls[p]:
            changed[p] = {**members, 'Document.xml': new_xml.encode('utf-8')}

    # paranoia scan of the FINAL state for every scanned document
    for p, members in docs.items():
        final_members = changed.get(p, members)
        try:
            has_saved_error = fcstd.validate_supported_references(final_members)
        except fcstd.FCStdError as exc:
            fail(f'{os.path.relpath(p)}: {exc}')
        if has_saved_error:
            warn(f'{os.path.relpath(p)}: has saved recompute errors '
                 f'mentioning .FCStd (stale "Link broken!" messages?); '
                 f'left as-is, recompute and save in FreeCAD to clear')

    # final in-memory resolution check against the simulated final layout
    final_files = set(final_path.values()) | set(plain_moves.values())
    for p, members in docs.items():
        final_members = changed.get(p, members)
        xml = fcstd.document_xml(final_members)
        for ref in fcstd.xlink_refs(xml):
            target = fcstd.resolve_ref(final_path[p], ref)
            existing_unmoved = target not in all_moves and target_exists.get(target, False)
            if target not in final_files and not existing_unmoved:
                fail(f'{os.path.relpath(final_path[p])}: link {ref!r} would '
                     f'not resolve after the move')

    # -- Phase 3: filesystem commit / verification
    for src, d in sorted(all_moves.items()):
        os.makedirs(os.path.dirname(d), exist_ok=True)

    n_rewritten = 0
    for p, members in changed.items():
        fcstd.write_fcstd(p, members)  # rewrite in place first (src still exists)
        n_rewritten += 1

    # two-phase move so overlapping src/dst sets (e.g. b->c with a->b) never
    # clobber
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

    # verify from disk: reread the resulting tree and check links
    for p in find_fcstd(root):
        members = fcstd.read_fcstd(p)
        for ref in fcstd.xlink_refs(fcstd.document_xml(members)):
            target = fcstd.resolve_ref(os.path.abspath(p), ref)
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
    try:
        if args.cmd == 'show':
            cmd_show(args.paths)
        elif args.cmd == 'rshow':
            cmd_rshow(args.paths, args.root)
        else:
            if len(args.paths) < 2:
                fail('mv needs at least one source and a destination')
            cmd_mv(args.paths[:-1], args.paths[-1], args.root)
    except fcstd.FCStdError as exc:
        fail(str(exc))


if __name__ == '__main__':
    main()
