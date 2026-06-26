#!/usr/bin/env python3
# res00edit - pack/unpack S2 (SonSilah / District 187) .Res00 + .ResDt archives.
# MIT License - Copyright (c) 2026 leftspace89. See LICENSE.
"""Pack a folder into an S2 .Res00 index + .ResDt data pair, or unpack one.

Format reference and details: see README.md.
"""

import argparse
import os
import struct
import sys
import zlib

MAGIC = 0x4B454248          # "HBEK"
VERSION = 2
HEADER_FMT = "<8I16s"       # magic, ver, strsize, nodes, files, f0, f1, f2, guid
HEADER_SIZE = 48
RECORD_FMT = "<IQQQIQQ"     # name, data_off, comp, uncomp, flags, seg2_off, seg2_size
RECORD_SIZE = 48
# Large files can be stored in two pieces: comp bytes at data_off, then
# seg2_size bytes at seg2_off, concatenated and inflated as one zlib stream.
# (Engine: CInCompressedStream::Read / sub_42F788 in TheRaw.exe.) The packer
# always writes a single segment, leaving these two trailing fields zero.
NODE_FMT = "<Iiii"          # name, child, next, record_count
NODE_SIZE = 16

FLAG_STORED = 0
FLAG_ZLIB = 9

DELETE_DIR = ".deleteDirectory"
CRC_DIR = "CRC"
SEP_MODES = ("backslash", "forward", "all")


class _Progress:
    """Minimal, dependency-free console progress bar (drawn on stderr).

    Animates only on a real terminal so redirected output stays clean; the
    final per-command summary is printed regardless.
    """

    def __init__(self, total, label, enabled=True, width=34):
        self.total = total
        self.label = label
        self.width = width
        self.n = 0
        self._last = -1
        self.enabled = enabled and total > 0 and sys.stderr.isatty()

    def update(self, n=1):
        self.n += n
        if not self.enabled:
            return
        pct = self.n * 100 // self.total
        if pct == self._last:
            return
        self._last = pct
        filled = self.width * self.n // self.total
        bar = "#" * filled + "-" * (self.width - filled)
        sys.stderr.write("\r%s [%s] %3d%% (%d/%d)"
                         % (self.label, bar, pct, self.n, self.total))
        sys.stderr.flush()

    def done(self):
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


def guid_string(guid):
    """Format a 16-byte GUID the way the engine names its .crc file."""
    d1, d2, d3 = struct.unpack_from("<IHH", guid, 0)
    return "%08x%04x%04x" % (d1, d2, d3) + "".join("%02x" % b for b in guid[8:16])


class StringTable:
    """Pools identical names; offset 0 is the root's empty name."""

    def __init__(self):
        self._buf = bytearray(b"\x00")
        self._map = {"": 0}

    def add(self, s):
        off = self._map.get(s)
        if off is None:
            off = len(self._buf)
            self._buf += s.encode("latin1") + b"\x00"
            self._map[s] = off
        return off

    def data(self):
        buf = bytes(self._buf)
        return buf + b"\x00" * ((-len(buf)) % 4)


# --------------------------------------------------------------------------- #
#  Packing
# --------------------------------------------------------------------------- #
def _scan(root):
    """Walk root -> {folder_path ('\\'-sep, ''=root): [(name, abspath), ...]}."""
    folders = {}

    def build(disk_path, rel):
        files, subdirs = [], []
        for name in sorted(os.listdir(disk_path), key=str.lower):
            full = os.path.join(disk_path, name)
            if os.path.isdir(full):
                subdirs.append((name, full))
            elif os.path.isfile(full):
                files.append((name, full))
        folders[rel] = files
        for name, full in subdirs:
            build(full, (rel + "\\" + name) if rel else name)

    build(root, "")
    return folders


def _sep_variants(path, mode):
    """Separator spellings of a folder path. 'all' yields every backslash-prefix
    / forward-slash-suffix split so a request matches wherever the engine's
    '\\'-base ends and the content's '/'-ref begins."""
    if not path:
        return [""]
    comps = path.split("\\")
    nsep = len(comps) - 1
    if nsep == 0 or mode == "backslash":
        return [path]
    if mode == "forward":
        return ["/".join(comps)]
    out = []
    for k in range(nsep + 1):
        s = comps[0]
        for i in range(1, len(comps)):
            s += ("\\" if (i - 1) < k else "/") + comps[i]
        out.append(s)
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _build_crc_manifest(paths):
    """Bytes of the CRC manifest the engine validates: u32 count, u32 pool size
    (= sum of nameLen+1), then count*[u16 nameLen, name, u32 crc]. Both headers
    must be exact or the validator's buffer overruns. crc is left 0, as in retail."""
    pool = sum(len(p.encode("latin1")) + 1 for p in paths)
    out = bytearray(struct.pack("<II", len(paths), pool))
    for path in paths:
        raw = path.encode("latin1")
        out += struct.pack("<H", len(raw)) + raw + struct.pack("<I", 0)
    return bytes(out)


def pack(input_dir, res00_path, resdt_path, compress=True, guid=None,
         sep_mode="all", make_crc=True, verbose=True):
    """Pack input_dir into res00_path + resdt_path."""
    if guid is None:
        guid = os.urandom(16)
    if len(guid) != 16:
        raise ValueError("guid must be 16 bytes")
    if sep_mode not in SEP_MODES:
        raise ValueError("sep_mode must be one of %r" % (SEP_MODES,))

    folders = _scan(input_dir)

    # write each blob once; records are mutable so the CRC record can be patched
    resdt = open(resdt_path, "wb")
    data_offset = 0
    total_in = total_out = 0
    folder_recs = {}
    crc_rec = None
    bar = _Progress(sum(len(v) for v in folders.values()), "packing", verbose)
    try:
        for canon in folders:
            recs = []
            for fname, fpath in folders[canon]:
                with open(fpath, "rb") as fh:
                    raw = fh.read()
                uncomp = len(raw)
                if compress and uncomp > 0:
                    blob = zlib.compress(raw, 9)
                    flags = FLAG_ZLIB if len(blob) < uncomp else FLAG_STORED
                    if flags == FLAG_STORED:
                        blob = raw
                else:
                    blob = raw
                    flags = FLAG_ZLIB if uncomp == 0 else FLAG_STORED
                recs.append([fname, data_offset, len(blob), uncomp, flags])
                resdt.write(blob)
                data_offset += len(blob)
                total_in += uncomp
                total_out += len(blob)
                bar.update()
            if not recs and canon != "":
                recs.append([DELETE_DIR, 0, 0, 0, FLAG_ZLIB])
            folder_recs[canon] = recs

        if make_crc:
            crc_rec = [guid_string(guid) + ".crc", 0, 0, 0, FLAG_STORED]
            folder_recs.setdefault(CRC_DIR, [])
            folder_recs[CRC_DIR] = [r for r in folder_recs[CRC_DIR]
                                    if r[0] != DELETE_DIR]
            folder_recs[CRC_DIR].append(crc_rec)

        for recs in folder_recs.values():
            recs.sort(key=lambda r: r[0].lower())

        # node list with separator variants, sorted globally for binary search
        class N:
            __slots__ = ("path", "canon", "recs", "idx")
        nodes = []
        for canon, recs in folder_recs.items():
            for variant in _sep_variants(canon, sep_mode):
                n = N()
                n.path, n.canon, n.recs, n.idx = variant, canon, recs, -1
                nodes.append(n)
        nodes.sort(key=lambda n: n.path.lower())
        for i, n in enumerate(nodes):
            n.idx = i

        if make_crc:
            ordered = [((n.path + "\\" + r[0]) if n.path else r[0])
                       for n in nodes for r in n.recs]
            manifest = _build_crc_manifest(ordered)
            crc_rec[1:5] = [data_offset, len(manifest), len(manifest), FLAG_STORED]
            resdt.write(manifest)
            data_offset += len(manifest)
    finally:
        resdt.close()
        bar.done()

    # child/next tree over canonical nodes only (lookup ignores it)
    children = {}
    for n in nodes:
        if n.path != n.canon or n.canon == "":
            continue
        parent = n.canon.rsplit("\\", 1)[0] if "\\" in n.canon else ""
        children.setdefault(parent, []).append(n.idx)
    for kids in children.values():
        kids.sort()
    child_of = {p: ks[0] for p, ks in children.items()}
    next_of = {}
    for ks in children.values():
        for a, b in zip(ks, ks[1:]):
            next_of[a] = b

    strtab = StringTable()
    records = bytearray()
    node_table = bytearray()
    file_count = 0
    for n in nodes:
        is_canon = (n.path == n.canon)
        child = child_of.get(n.canon, -1) if is_canon else -1
        nxt = next_of.get(n.idx, -1) if is_canon else -1
        node_table += struct.pack(NODE_FMT, strtab.add(n.path), child, nxt, len(n.recs))
        for fname, off, comp, unc, flags in n.recs:
            records += struct.pack(RECORD_FMT, strtab.add(fname), off, comp, unc,
                                   flags, 0, 0)
            file_count += 1

    strdata = strtab.data()
    header = struct.pack(HEADER_FMT, MAGIC, VERSION, len(strdata), len(nodes),
                         file_count, 0, 0, 1, guid)
    with open(res00_path, "wb") as out:
        out.write(header)
        out.write(strdata)
        out.write(records)
        out.write(node_table)

    if verbose:
        ratio = (100.0 * total_out / total_in) if total_in else 100.0
        nvar = len(nodes) - len(folder_recs)
        print("packed %d folders (%d nodes incl. %d separator variants), %d records"
              % (len(folder_recs), len(nodes), nvar, file_count))
        if make_crc:
            print("  CRC manifest: CRC\\%s" % crc_rec[0])
        print("  %s  (%d bytes)" % (res00_path, os.path.getsize(res00_path)))
        print("  %s  (%d bytes, %.1f%% of %d uncompressed)"
              % (resdt_path, data_offset, ratio, total_in))


# --------------------------------------------------------------------------- #
#  Reading
# --------------------------------------------------------------------------- #
class Record:
    __slots__ = ("name", "data_offset", "comp_size", "uncomp_size", "flags",
                 "seg2_offset", "seg2_size")

    def __init__(self, name, data_offset, comp_size, uncomp_size, flags,
                 seg2_offset=0, seg2_size=0):
        self.name = name
        self.data_offset = data_offset
        self.comp_size = comp_size
        self.uncomp_size = uncomp_size
        self.flags = flags
        self.seg2_offset = seg2_offset
        self.seg2_size = seg2_size


class Node:
    __slots__ = ("path", "child", "next", "record_count")

    def __init__(self, path, child, nxt, rc):
        self.path = path
        self.child = child
        self.next = nxt
        self.record_count = rc


class Archive:
    def __init__(self, res00_path):
        with open(res00_path, "rb") as fh:
            blob = fh.read()
        (magic, ver, strsize, ncount, fcount,
         self.flag0, self.flag1, self.flag2, self.guid) = struct.unpack_from(HEADER_FMT, blob, 0)
        if magic != MAGIC:
            raise ValueError("not an S2 Res00 archive (magic %08x)" % magic)
        if ver != VERSION:
            raise ValueError("unsupported version %d" % ver)

        off = HEADER_SIZE
        strtab = blob[off:off + strsize]
        off += strsize

        def cstr(o):
            return strtab[o:strtab.find(b"\x00", o)].decode("latin1")

        self.records = []
        for _ in range(fcount):
            no, doff, comp, unc, fl, s2off, s2sz = struct.unpack_from(RECORD_FMT, blob, off)
            self.records.append(Record(cstr(no), doff, comp, unc, fl, s2off, s2sz))
            off += RECORD_SIZE

        self.nodes = []
        for _ in range(ncount):
            no, child, nxt, rc = struct.unpack_from(NODE_FMT, blob, off)
            self.nodes.append(Node(cstr(no), child, nxt, rc))
            off += NODE_SIZE

    def entries(self, dedupe=False):
        """Yield (folder_path, Record) per real file. dedupe collapses the
        packer's separator-variant aliases to one '\\'-separated entry each."""
        seen = set()
        ri = 0
        for node in self.nodes:
            for _ in range(node.record_count):
                rec = self.records[ri]
                ri += 1
                if rec.name == DELETE_DIR:
                    continue
                if dedupe:
                    key = (node.path.replace("/", "\\").lower(),
                           rec.name.lower(), rec.data_offset)
                    if key in seen:
                        continue
                    seen.add(key)
                    yield node.path.replace("/", "\\"), rec
                else:
                    yield node.path, rec


def _read_blob(resdt, rec):
    if rec.uncomp_size == 0:
        return b""
    resdt.seek(rec.data_offset)
    data = resdt.read(rec.comp_size)
    if rec.seg2_size:
        # split storage: remainder of the compressed stream lives elsewhere
        resdt.seek(rec.seg2_offset)
        data += resdt.read(rec.seg2_size)
    if rec.flags != FLAG_STORED:
        return zlib.decompress(data)
    return data


def unpack(res00_path, resdt_path, out_dir, verbose=True):
    arc = Archive(res00_path)
    entries = list(arc.entries(dedupe=True))
    bar = _Progress(len(entries), "extracting", verbose)
    with open(resdt_path, "rb") as resdt:
        n = 0
        for folder, rec in entries:
            rel = (folder + "\\" + rec.name) if folder else rec.name
            dest = os.path.join(out_dir, rel.replace("\\", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            data = _read_blob(resdt, rec)
            if len(data) != rec.uncomp_size:
                raise ValueError("size mismatch on %s (%d != %d)"
                                 % (rel, len(data), rec.uncomp_size))
            with open(dest, "wb") as out:
                out.write(data)
            n += 1
            bar.update()
        bar.done()
        for node in arc.nodes:
            p = node.path.replace("/", "\\")
            if p:
                os.makedirs(os.path.join(out_dir, p.replace("\\", os.sep)),
                            exist_ok=True)
    if verbose:
        print("extracted %d files to %s" % (n, out_dir))


def info(res00_path):
    arc = Archive(res00_path)
    nfiles = sum(1 for _ in arc.entries())
    nuniq = sum(1 for _ in arc.entries(dedupe=True))
    sorted_ok = all(arc.nodes[i].path.lower() <= arc.nodes[i + 1].path.lower()
                    for i in range(len(arc.nodes) - 1))
    print("S2 Res00 archive: %s" % res00_path)
    print("  guid          : %s" % arc.guid.hex())
    print("  flags         : %d,%d,%d" % (arc.flag0, arc.flag1, arc.flag2))
    print("  nodes         : %d (sorted for binary search: %s)"
          % (len(arc.nodes), sorted_ok))
    print("  file records  : %d (%d real, %d unique physical files)"
          % (len(arc.records), nfiles, nuniq))
    comp = sum(r.comp_size + r.seg2_size for r in arc.records)
    unc = sum(r.uncomp_size for r in arc.records)
    nsplit = sum(1 for r in arc.records if r.seg2_size)
    print("  data size     : %d bytes compressed / %d uncompressed" % (comp, unc))
    if nsplit:
        print("  split records : %d (compressed stream stored in two segments)" % nsplit)
    print("  sample entries:")
    for i, (folder, rec) in enumerate(arc.entries(dedupe=True)):
        if i >= 12:
            break
        rel = (folder + "\\" + rec.name) if folder else rec.name
        tag = "zlib" if rec.flags == FLAG_ZLIB else "store"
        print("    %-50s %8d -> %8d  %s" % (rel, rec.uncomp_size, rec.comp_size, tag))


# --------------------------------------------------------------------------- #
#  CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(
        description="Pack/unpack S2 (.Res00 + .ResDt) archives.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pack", help="build a Res00/ResDt pair from a folder")
    sp.add_argument("input_dir")
    sp.add_argument("res00", help="output .Res00 path")
    sp.add_argument("resdt", nargs="?", help="output .ResDt path (default: alongside res00)")
    sp.add_argument("--store", action="store_true", help="store uncompressed (no zlib)")
    sp.add_argument("--sep", choices=SEP_MODES, default="all",
                    help="separator spellings stored per folder path. "
                         "'all' (default) stores every '\\'-prefix / '/'-suffix "
                         "split so both backslash and forward-slash refs resolve; "
                         "'backslash' = retail-style only; 'forward' = '/' only.")
    sp.add_argument("--no-crc", action="store_true",
                    help="do not generate the CRC\\<guid>.crc manifest the engine "
                         "looks for (omitting it only causes a harmless log warning).")

    su = sub.add_parser("unpack", help="extract a Res00/ResDt pair to a folder")
    su.add_argument("res00")
    su.add_argument("resdt")
    su.add_argument("out_dir")

    si = sub.add_parser("info", help="print archive metadata")
    si.add_argument("res00")

    args = p.parse_args(argv)

    if args.cmd == "pack":
        resdt = args.resdt
        if resdt is None:
            base = args.res00
            for ext in (".Res00", ".res00"):
                if base.endswith(ext):
                    base = base[:-len(ext)]
                    break
            resdt = base + ".ResDt"
        pack(args.input_dir, args.res00, resdt, compress=not args.store,
             sep_mode=args.sep, make_crc=not args.no_crc)
    elif args.cmd == "unpack":
        unpack(args.res00, args.resdt, args.out_dir)
    elif args.cmd == "info":
        info(args.res00)
    return 0


if __name__ == "__main__":
    sys.exit(main())
