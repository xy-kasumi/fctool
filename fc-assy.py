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

Each .FCStd archive is loaded and validated independently. An archive the
tool cannot safely understand (malformed ZIP, ".FCStd" byte sequences
outside XLink file attributes, absolute stored links, ...) is recorded as
a per-document load error and only fails a command when it is explicitly
requested, reached by show traversal, or otherwise needed by mv. Unrelated
broken archives never abort an operation, and mv never rewrites an archive
it could not load.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Iterable

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


# ---------------------------------------------------------------- workspace

class Workspace:
    """Tolerant index of .FCStd documents under a scan root.

    Successfully loaded documents are cached by absolute path, load errors
    are retained by absolute path (one bad archive never aborts preparation),
    and a reverse ``absolute target -> referring documents`` index is built
    from the successfully loaded documents' links only. A malformed file
    contributes no reverse edges because its links are unknowable.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self.documents: dict[str, fcstd.FCStdDocument] = {}
        self.errors: dict[str, fcstd.FCStdError] = {}
        self._referrers: dict[str, set[str]] = {}

    def _index(self, path: str, doc: fcstd.FCStdDocument) -> None:
        """Cache a successfully loaded document and its reverse edges."""
        self.documents[path] = doc
        for target in doc.links():
            self._referrers.setdefault(target, set()).add(path)

    def document(self, path: str) -> fcstd.FCStdDocument | None:
        """Return the cached document for ``path``.

        Returns ``None`` for a missing path, re-raises a cached load error,
        and loads/caches an existing (possibly out-of-root) target on demand
        so that ``show`` keeps its recursive behavior.
        """
        abspath = os.path.abspath(path)
        if abspath in self.documents:
            return self.documents[abspath]
        if abspath in self.errors:
            raise self.errors[abspath]
        if not os.path.isfile(abspath):
            return None
        try:
            doc = fcstd.FCStdDocument.read(abspath)
        except fcstd.FCStdError as exc:
            self.errors[abspath] = exc
            raise
        self._index(abspath, doc)
        return doc

    def referrers(self, path: str) -> set[str]:
        """Absolute paths of successfully indexed documents linking to ``path``."""
        return set(self._referrers.get(os.path.abspath(path), ()))


def prepare_workspace(root: str,
                      extra_paths: Iterable[str] = ()) -> Workspace:
    """Scan ``root`` once and index every .FCStd archive it contains.

    Requested existing ``extra_paths`` outside the root are included too.
    Every candidate is loaded independently; a load failure is recorded on
    the workspace and does not abort preparation. No filesystem access
    happens here beyond the scan and the archive reads.
    """
    root = os.path.abspath(root)
    ws = Workspace(root)
    candidates = [os.path.abspath(p) for p in find_fcstd(root)]
    for p in extra_paths:
        abspath = os.path.abspath(p)
        if abspath not in candidates and os.path.isfile(abspath):
            candidates.append(abspath)
    for abspath in candidates:
        try:
            doc = fcstd.FCStdDocument.read(abspath)
        except fcstd.FCStdError as exc:
            ws.errors[abspath] = exc
        else:
            ws._index(abspath, doc)
    return ws


def atomic_write(path: str, data: bytes) -> None:
    """Atomically write opaque serialized archive bytes to ``path``."""
    tmp = path + '.fc-assy.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------- show

def cmd_show(paths):
    # Filter input paths (existence check: command I/O)
    requested = []
    for p in paths:
        if not p.endswith('.FCStd') or not os.path.isfile(p):
            warn(f'{p}: not an existing .FCStd file, ignored')
            continue
        requested.append(os.path.abspath(p))
    # Forward traversal needs no global reverse-reference index. Load only
    # requested documents and dependencies actually reached from them.
    ws = Workspace(os.path.abspath(os.getcwd()))
    for abspath in requested:
        show_tree(abspath, depth=0, seen=set(), ws=ws)


def show_tree(abspath, depth, seen, ws):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    doc = ws.document(abspath)  # may raise FCStdError -> top-level diagnostic
    if doc is None:
        print(f'{indent}{rel}  [MISSING]')
        return
    kind = doc.classify()
    if abspath in seen:
        print(f'{indent}{rel}  [{kind}] (already shown)')
        return
    print(f'{indent}{rel}  [{kind}]')
    seen.add(abspath)
    # traverse links directly; classification is a display hint only
    for target in sorted(doc.links()):
        show_tree(target, depth + 1, seen, ws)


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

    # The reverse XLink index covers documents inside the scan root (plus
    # the requested extras); the requested file itself need not be under it.
    ws = prepare_workspace(root, requested)

    for abspath in requested:
        rshow_tree(abspath, depth=0, seen=set(), ws=ws)


def rshow_tree(abspath, depth, seen, ws):
    rel = os.path.relpath(abspath)
    indent = '  ' * depth
    doc = ws.document(abspath)  # may raise FCStdError -> top-level diagnostic
    if doc is None:
        print(f'{indent}{rel}  [MISSING]')
        return
    kind = doc.classify()
    if abspath in seen:
        print(f'{indent}{rel}  [{kind}] (already shown)')
        return
    print(f'{indent}{rel}  [{kind}]')
    seen.add(abspath)
    for target in sorted(ws.referrers(abspath)):
        rshow_tree(target, depth + 1, seen, ws)


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

    ws = prepare_workspace(root)

    # -- Phase 2: in-memory plan / validation (no filesystem changes)
    # Access every moved FCStd source, surfacing its stored load error;
    # then add every successfully indexed referrer of a moved FCStd path.
    # Unrelated flagged archives are never accessed here.
    affected = {}  # abs path -> FCStdDocument (moved sources + referrers)
    for src in sorted(moves):
        doc = ws.document(src)  # re-raises a cached FCStdError, if any
        if doc is None:
            fail(f'{os.path.relpath(src)}: no such file or directory')
        affected[src] = doc
    for src in sorted(moves):
        for referrer in sorted(ws.referrers(src)):
            if referrer not in affected:
                affected[referrer] = ws.document(referrer)
    final_path = {p: moves.get(p, p) for p in affected}

    # CLI policy before mutation: every link in an affected document must
    # resolve to an existing file (relink/serialize never check this).
    for p in sorted(affected):
        for target in affected[p].links():
            if not os.path.isfile(target):  # existence check: command policy
                fail(f'{os.path.relpath(p)}: pre-existing broken XLink to '
                     f'{os.path.relpath(target)}; fix that first')

    # informational stale-recompute-error warning, affected documents only
    for p in sorted(affected):
        if affected[p].has_saved_path_error:
            warn(f'{os.path.relpath(p)}: has saved recompute errors '
                 f'mentioning .FCStd (stale "Link broken!" messages?); '
                 f'left as-is, recompute and save in FreeCAD to clear')

    # rewrite every internal occurrence of each moved FCStd path; successful
    # load is the sole schema-safety gate (combined/unknown are not refused)
    for p in sorted(affected):
        for src, d in sorted(moves.items()):
            affected[p].relink(src, d)

    # serialize every affected document against its FINAL location; moving a
    # document rebases its serialized relative outgoing paths even when their
    # absolute targets did not move
    serialized = {}  # abs pre-move path -> opaque complete archive bytes
    for p in sorted(affected):
        try:
            serialized[p] = affected[p].serialize(final_path[p])
        except fcstd.FCStdError as exc:
            fail(f'{os.path.relpath(final_path[p])}: {exc}')

    # validate links of affected documents against the simulated final layout
    final_files = set(all_moves.values())
    for p in sorted(affected):
        for target in affected[p].links():
            final_target = all_moves.get(target, target)
            if final_target not in final_files and not os.path.isfile(final_target):
                fail(f'{os.path.relpath(final_path[p])}: link to '
                     f'{os.path.relpath(target)} would not resolve after '
                     f'the move')

    # -- Phase 3: filesystem commit / verification
    for d in all_moves.values():
        os.makedirs(os.path.dirname(d), exist_ok=True)

    # two-phase staging so overlapping src/dst sets (e.g. b->c with a->b)
    # never clobber each other
    stage_of = {}
    for i, (src, d) in enumerate(sorted(all_moves.items())):
        tmp = os.path.join(os.path.dirname(src), f'.fc-assy.stage.{i}')
        os.replace(src, tmp)
        stage_of[src] = tmp

    # moved FCStd files: atomically write the precomputed serialized bytes
    # directly to their destination, then drop the staged original
    for src in sorted(moves):
        atomic_write(moves[src], serialized[src])
        os.remove(stage_of[src])
    # plain (non-FCStd) files move normally
    for src in sorted(plain_moves):
        os.replace(stage_of[src], plain_moves[src])
    # affected referrers that did not move are atomically replaced
    for p in sorted(affected):
        if p not in moves:
            atomic_write(p, serialized[p])

    for src in sorted(all_moves, reverse=True):  # prune emptied dirs
        sd = os.path.dirname(src)
        while sd != root:
            try:
                os.rmdir(sd)
            except OSError:
                break
            sd = os.path.dirname(sd)

    # verify from disk: reload only the affected documents at their final
    # paths and check their links. Unrelated flagged archives and unrelated
    # pre-existing broken links are never looked at here.
    for p in sorted(affected):
        final = final_path[p]
        try:
            doc = fcstd.FCStdDocument.read(final)
        except fcstd.FCStdError as exc:
            fail(f'POST-CHECK FAILED: {final}: could not reload ({exc}). '
                 f'Restore from version control and report this.')
        for target in doc.links():
            if not os.path.isfile(target):
                fail(f'POST-CHECK FAILED: {final}: link to {target!r} does '
                     f'not resolve. Restore from version control and '
                     f'report this.')

    print(f'moved {len(all_moves)} file(s), '
          f'rewrote links in {len(serialized)} document(s); all links verified')


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