#!/usr/bin/env python3
"""
inventory.py - Step 1 of the gold drill-core dataset preparation.

What it does (READ-ONLY on the source folder - it never changes, moves or
copies your original files):
  * walks the source folder recursively
  * records path, name, type, size, modified time and SHA-256 for every file
  * reads capture time, camera model and image size from .CR2 headers
  * assigns each file to a drill hole and core box from its folder/file names
  * flags: empty / unreadable / corrupted / truncated RAW files, exact
    duplicates, drill-hole or box names that are missing, ambiguous or
    conflicting, gaps in box numbering, drill holes without a journal
  * compares checksums with the previous run (if any) to detect changed,
    missing or new files

Only the Python standard library is used (Python 3.6+). No internet needed.

Usage (Windows Command Prompt):
    python inventory.py "D:\\path\\to\\source_files" "D:\\path\\to\\gold_core_dataset"

Outputs (inside the output folder):
    metadata/source_inventory.csv          one row per source file
    metadata/sha256sums.txt                checksums in standard format
    metadata/inventory_run_info.json       settings, versions, counts
    metadata/history/                      previous inventories (auto-archived)
    quality_control/validation_report.csv  all issues found
    quality_control/checksum_verification.csv  (only when a previous run exists)

Before the first run: copy config.example.json to config.json and enter the
drill hole IDs. Adjust "box_patterns" there if your box naming is different.
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_VERSION = "0.1.0"

# =============================================================================
# CONFIG - edit here if needed
# =============================================================================

# Project-specific settings (drill hole IDs, optional box patterns) are read
# from config.json next to this script, so that confidential IDs are never
# stored in the code repository. Copy config.example.json -> config.json and
# edit it. config.json is listed in .gitignore.
#
# config.json keys:
#   "drillholes":   {"<ID>": ["<ID>", "<alternative spelling>", ...], ...}
#                   Matching ignores case and treats "-", " " and "_" alike.
#   "box_patterns": optional list of regular expressions replacing the
#                   default BOX_PATTERNS below.
DRILLHOLE_ALIASES = {}

# Default regular expressions that find a box number in a folder or file name.
# The first group must capture the number. Examples matched:
#   box_012, Box12, BX-3, ящик 7, Ящ_07, box#4, box №5
BOX_PATTERNS = [
    r"(?<![a-zа-яё])(?:box|bx|ящик|ящ)[\s_\-\.#№]*0*(\d{1,4})(?!\d)",
]

# File types by extension (lowercase).
FILE_TYPES = {
    ".cr2": "raw_image",
    ".cr3": "raw_image",
    ".jpg": "image", ".jpeg": "image", ".tif": "image", ".tiff": "image",
    ".png": "image",
    ".xlsm": "journal", ".xlsx": "journal", ".xls": "journal",
    ".doc": "reference_document", ".docx": "reference_document",
    ".pdf": "reference_document", ".txt": "reference_document",
    ".rtf": "reference_document", ".odt": "reference_document",
}

# System files that are skipped entirely (lowercase names).
IGNORE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}
IGNORE_PREFIXES = ("~$", "._")  # Office lock files, macOS resource forks

HASH_CHUNK_BYTES = 8 * 1024 * 1024

# =============================================================================

IMAGE_TYPES = {"raw_image", "image"}

INVENTORY_COLUMNS = [
    "file_id", "relative_path", "filename", "extension", "file_type",
    "size_bytes", "modified_time", "sha256",
    "drillhole_id", "drillhole_source", "box_number", "box_source",
    "capture_datetime", "camera_make", "camera_model",
    "width_px", "height_px", "dimension_source", "header_status",
    "duplicate_group", "duplicate_count", "qc_flags",
]

REPORT_COLUMNS = [
    "issue_id", "record_id", "relative_path", "issue_type", "severity",
    "details", "review_status",
]


# -----------------------------------------------------------------------------
# Name parsing
# -----------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"[\s\-]+", "_", text.lower())


_DH_REGEXES = []
_BOX_REGEXES = []
_BARE_NUMBER = re.compile(r"^0*(\d{1,4})$")


def load_config(path):
    """Read config.json and build the name-matching rules."""
    global DRILLHOLE_ALIASES, BOX_PATTERNS
    if not path.exists():
        sys.exit("ERROR: config file not found: %s\n"
                 "Copy config.example.json to config.json (same folder as this script)\n"
                 "and put your drill hole IDs in it." % path)
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    dhs = cfg.get("drillholes") or {}
    if not isinstance(dhs, dict) or not dhs:
        sys.exit("ERROR: config.json must contain a non-empty \"drillholes\" object.")
    DRILLHOLE_ALIASES = {k: (list(v) if v else [k]) for k, v in dhs.items()}
    if cfg.get("box_patterns"):
        BOX_PATTERNS = list(cfg["box_patterns"])

    del _DH_REGEXES[:]
    for dh, aliases in DRILLHOLE_ALIASES.items():
        for alias in set(aliases) | {dh}:
            _DH_REGEXES.append((
                dh,
                re.compile(r"(?<![0-9a-z])" + re.escape(_norm(alias)) + r"(?![0-9a-z])"),
            ))
    del _BOX_REGEXES[:]
    for p in BOX_PATTERNS:
        try:
            _BOX_REGEXES.append(re.compile(p, re.IGNORECASE))
        except re.error as exc:
            sys.exit("ERROR: invalid box pattern %r in config: %s" % (p, exc))


def find_drillholes(text):
    t = _norm(text)
    return {dh for dh, rx in _DH_REGEXES if rx.search(t)}


def find_boxes(text, bare_number_is_box=False):
    found = set()
    for rx in _BOX_REGEXES:
        for m in rx.finditer(text):
            found.add(int(m.group(1)))
    if bare_number_is_box:
        m = _BARE_NUMBER.match(text.strip())
        if m:
            found.add(int(m.group(1)))
    return found


def resolve(folder_vals, file_vals):
    """Combine values found in folder names and in the file name.

    Returns (value, source, problem) where problem is None, 'ambiguous'
    or 'conflict'. Nothing is guessed: on any disagreement value is ''.
    """
    if len(folder_vals) > 1 or len(file_vals) > 1:
        return "", "", "ambiguous"
    fo = next(iter(folder_vals)) if folder_vals else None
    fi = next(iter(file_vals)) if file_vals else None
    if fo is not None and fi is not None:
        if fo != fi:
            return "", "", "conflict"
        return fo, "folder+filename", None
    if fi is not None:
        return fi, "filename", None
    if fo is not None:
        return fo, "folder", None
    return "", "", None


# -----------------------------------------------------------------------------
# TIFF / CR2 header reading (standard library only)
# -----------------------------------------------------------------------------

TAG_WIDTH, TAG_HEIGHT = 0x0100, 0x0101
TAG_MAKE, TAG_MODEL, TAG_DATETIME = 0x010F, 0x0110, 0x0132
TAG_STRIP_OFFSETS, TAG_STRIP_COUNTS = 0x0111, 0x0117
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_PIXEL_X, TAG_PIXEL_Y = 0xA002, 0xA003


class HeaderError(Exception):
    pass


def read_tiff_header(path, expect_cr2):
    """Return dict with make, model, capture_datetime, width, height,
    dimension_source, and a list of problems. Raises HeaderError for an
    unreadable/invalid header."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        head = f.read(16)
        if len(head) < 16:
            raise HeaderError("file too small for a TIFF/CR2 header")
        if head[:2] == b"II":
            e = "<"
        elif head[:2] == b"MM":
            e = ">"
        else:
            raise HeaderError("not a TIFF/CR2 file (bad byte-order mark)")
        if struct.unpack(e + "H", head[2:4])[0] != 42:
            raise HeaderError("not a TIFF/CR2 file (bad magic number)")
        if expect_cr2 and head[8:10] != b"CR":
            raise HeaderError("TIFF header present but no Canon CR2 signature")

        def read_ifd(offset):
            if offset < 8 or offset + 2 > size:
                raise HeaderError("IFD offset outside file (truncated?)")
            f.seek(offset)
            n = struct.unpack(e + "H", f.read(2))[0]
            if n == 0 or n > 2000 or offset + 2 + 12 * n + 4 > size:
                raise HeaderError("invalid IFD entry count (corrupted?)")
            raw = f.read(12 * n)
            entries = {}
            for i in range(n):
                tag, typ, cnt, val = struct.unpack(e + "HHI4s", raw[i * 12:(i + 1) * 12])
                entries[tag] = (typ, cnt, val)
            nxt = struct.unpack(e + "I", f.read(4))[0]
            return entries, nxt

        def as_int(entry):
            typ, cnt, val = entry
            if typ == 3:
                return struct.unpack(e + "H", val[:2])[0]
            if typ == 4:
                return struct.unpack(e + "I", val)[0]
            return None

        def as_ascii(entry):
            typ, cnt, val = entry
            if typ != 2:
                return ""
            if cnt <= 4:
                raw = val[:cnt]
            else:
                off = struct.unpack(e + "I", val)[0]
                if off + cnt > size:
                    return ""
                f.seek(off)
                raw = f.read(cnt)
            return raw.split(b"\0")[0].decode("ascii", "replace").strip()

        ifd0, nxt = read_ifd(struct.unpack(e + "I", head[4:8])[0])

        info = {"make": "", "model": "", "capture_datetime": "",
                "width": "", "height": "", "dimension_source": "", "problems": []}
        if TAG_MAKE in ifd0:
            info["make"] = as_ascii(ifd0[TAG_MAKE])
        if TAG_MODEL in ifd0:
            info["model"] = as_ascii(ifd0[TAG_MODEL])

        exif = {}
        if TAG_EXIF_IFD in ifd0:
            try:
                exif, _ = read_ifd(as_int(ifd0[TAG_EXIF_IFD]))
            except HeaderError as exc:
                info["problems"].append("EXIF block unreadable: %s" % exc)

        stamp = ""
        if TAG_DATETIME_ORIGINAL in exif:
            stamp = as_ascii(exif[TAG_DATETIME_ORIGINAL])
        elif TAG_DATETIME in ifd0:
            stamp = as_ascii(ifd0[TAG_DATETIME])
        info["capture_datetime"] = exif_time_to_iso(stamp)

        if TAG_PIXEL_X in exif and TAG_PIXEL_Y in exif:
            info["width"], info["height"] = as_int(exif[TAG_PIXEL_X]), as_int(exif[TAG_PIXEL_Y])
            info["dimension_source"] = "exif_pixel_dimension"
        elif TAG_WIDTH in ifd0 and TAG_HEIGHT in ifd0:
            info["width"], info["height"] = as_int(ifd0[TAG_WIDTH]), as_int(ifd0[TAG_HEIGHT])
            info["dimension_source"] = "ifd0"

        # Walk the IFD chain and check that every image strip lies inside the
        # file. A strip that runs past the end means the file is truncated
        # (e.g. interrupted copy from the memory card).
        seen = set()
        ifds = [ifd0]
        while nxt and nxt not in seen and len(ifds) < 10:
            seen.add(nxt)
            try:
                ifd, nxt = read_ifd(nxt)
            except HeaderError as exc:
                info["problems"].append("IFD chain broken: %s" % exc)
                break
            ifds.append(ifd)
        for idx, ifd in enumerate(ifds):
            if TAG_STRIP_OFFSETS in ifd and TAG_STRIP_COUNTS in ifd:
                o, c = ifd[TAG_STRIP_OFFSETS], ifd[TAG_STRIP_COUNTS]
                if o[1] == 1 and c[1] == 1:
                    off, cnt = as_int(o), as_int(c)
                    if off is not None and cnt is not None and off + cnt > size:
                        info["problems"].append(
                            "TRUNCATED: image data in IFD%d ends at byte %d but file is %d bytes"
                            % (idx, off + cnt, size))
        return info


def exif_time_to_iso(stamp):
    if not stamp:
        return ""
    try:
        return dt.datetime.strptime(stamp, "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        return stamp  # keep as-is rather than guess


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def sha256_of(path, progress_label=""):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def stable_file_id(rel_posix):
    return "F" + hashlib.sha256(rel_posix.encode("utf-8")).hexdigest()[:12]


def is_inside(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel shows Cyrillic correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_previous_inventory(path):
    prev = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            prev[r["relative_path"]] = r
    return prev


def ranges(nums):
    """[3,4,5,9] -> ['3-5', '9']"""
    out, start, last = [], None, None
    for n in sorted(nums):
        if start is None:
            start = last = n
        elif n == last + 1:
            last = n
        else:
            out.append(str(start) if start == last else "%d-%d" % (start, last))
            start = last = n
    if start is not None:
        out.append(str(start) if start == last else "%d-%d" % (start, last))
    return out


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Inventory, checksums and file checks (read-only).")
    ap.add_argument("source", help="folder with the original files (never modified)")
    ap.add_argument("output", help="dataset folder where results are written")
    ap.add_argument("--bare-number-folders-are-boxes", action="store_true",
                    help="treat folders named only with a number (e.g. '012') as box numbers")
    ap.add_argument("--no-verify", action="store_true",
                    help="do not compare with the previous inventory")
    ap.add_argument("--config", default=str(Path(__file__).resolve().with_name("config.json")),
                    help="path to config.json (default: next to this script)")
    args = ap.parse_args(argv)
    load_config(Path(args.config))

    src = Path(args.source)
    out = Path(args.output)
    if not src.is_dir():
        sys.exit("ERROR: source folder not found: %s" % src)
    if is_inside(out, src):
        sys.exit("ERROR: output folder must not be inside the source folder "
                 "(the source must stay untouched).")
    if is_inside(src, out) and src.resolve() != out.resolve():
        pass  # e.g. source = gold_core_dataset/raw - allowed, raw is only read

    meta_dir = out / "metadata"
    qc_dir = out / "quality_control"
    meta_dir.mkdir(parents=True, exist_ok=True)
    qc_dir.mkdir(parents=True, exist_ok=True)

    started = dt.datetime.now()
    print("Inventory v%s - source: %s" % (SCRIPT_VERSION, src))

    # ---- collect files -----------------------------------------------------
    walk_errors = []
    files, skipped = [], 0
    for root, dirs, names in os.walk(str(src), onerror=walk_errors.append, followlinks=False):
        dirs.sort()
        if is_inside(Path(root), out):
            dirs[:] = []
            continue
        for name in sorted(names):
            low = name.lower()
            if low in IGNORE_NAMES or low.startswith(IGNORE_PREFIXES):
                skipped += 1
                continue
            files.append(Path(root) / name)
    total = len(files)
    total_bytes = 0
    print("Found %d files (%d system files skipped). Hashing..." % (total, skipped))

    issues = []

    def issue(record_id, rel, itype, severity, details):
        issues.append({
            "issue_id": "", "record_id": record_id, "relative_path": rel,
            "issue_type": itype, "severity": severity, "details": details,
            "review_status": "open",
        })

    for err in walk_errors:
        issue("", str(getattr(err, "filename", "")), "folder_unreadable", "error", str(err))

    rows = []
    for i, p in enumerate(files, 1):
        rel = p.relative_to(src)
        rel_posix = rel.as_posix()
        ext = p.suffix.lower()
        ftype = FILE_TYPES.get(ext, "other")
        fid = stable_file_id(rel_posix)
        flags = []
        row = {c: "" for c in INVENTORY_COLUMNS}
        row.update({"file_id": fid, "relative_path": rel_posix, "filename": p.name,
                    "extension": ext, "file_type": ftype})

        print("  [%d/%d] %s" % (i, total, rel_posix))

        # size, time, checksum
        try:
            st = p.stat()
            row["size_bytes"] = st.st_size
            total_bytes += st.st_size
            row["modified_time"] = dt.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
            row["sha256"] = sha256_of(p)
        except OSError as exc:
            issue(fid, rel_posix, "file_unreadable", "error", str(exc))
            flags.append("file_unreadable")
            row["qc_flags"] = ";".join(flags)
            rows.append(row)
            continue
        if st.st_size == 0:
            issue(fid, rel_posix, "empty_file", "error", "file size is 0 bytes")
            flags.append("empty_file")

        # drill hole / box from names
        folder_parts = list(rel.parts[:-1])
        stem = p.stem
        dh_folder = set().union(*[find_drillholes(x) for x in folder_parts]) if folder_parts else set()
        dh_file = find_drillholes(stem)
        dh, dh_src, dh_prob = resolve(dh_folder, dh_file)
        row["drillhole_id"], row["drillhole_source"] = dh, dh_src

        bx_folder = set().union(*[find_boxes(x, args.bare_number_folders_are_boxes)
                                  for x in folder_parts]) if folder_parts else set()
        bx_file = find_boxes(stem)
        bx, bx_src, bx_prob = resolve(bx_folder, bx_file)
        row["box_number"], row["box_source"] = bx, bx_src

        if dh_prob:
            issue(fid, rel_posix, "drillhole_" + dh_prob, "error",
                  "folder names suggest %s, file name suggests %s"
                  % (sorted(dh_folder) or "-", sorted(dh_file) or "-"))
            flags.append("drillhole_" + dh_prob)
        elif not dh and ftype in IMAGE_TYPES | {"journal"}:
            issue(fid, rel_posix, "drillhole_unassigned", "warning",
                  "no known drill-hole ID in folder or file name")
            flags.append("drillhole_unassigned")

        if ftype in IMAGE_TYPES:
            if bx_prob:
                issue(fid, rel_posix, "box_" + bx_prob, "error",
                      "folder names suggest box %s, file name suggests box %s"
                      % (sorted(bx_folder) or "-", sorted(bx_file) or "-"))
                flags.append("box_" + bx_prob)
            elif bx == "":
                issue(fid, rel_posix, "box_unassigned", "warning",
                      "no box number found in folder or file name")
                flags.append("box_unassigned")

        # RAW / TIFF header
        if st.st_size > 0 and ext in (".cr2", ".tif", ".tiff"):
            try:
                info = read_tiff_header(p, expect_cr2=(ext == ".cr2"))
                row["camera_make"] = info["make"]
                row["camera_model"] = info["model"]
                row["capture_datetime"] = info["capture_datetime"]
                row["width_px"] = info["width"]
                row["height_px"] = info["height"]
                row["dimension_source"] = info["dimension_source"]
                row["header_status"] = "ok" if not info["problems"] else "; ".join(info["problems"])
                for prob in info["problems"]:
                    if prob.startswith("TRUNCATED"):
                        issue(fid, rel_posix, "truncated_raw", "error", prob)
                        flags.append("truncated_raw")
                    else:
                        issue(fid, rel_posix, "raw_header_problem", "warning", prob)
                        flags.append("raw_header_problem")
                if not info["capture_datetime"]:
                    issue(fid, rel_posix, "missing_capture_time", "info",
                          "no capture date/time in the file header")
            except (HeaderError, OSError, struct.error) as exc:
                row["header_status"] = "invalid: %s" % exc
                issue(fid, rel_posix, "invalid_raw_header", "error", str(exc))
                flags.append("invalid_raw_header")
        elif ext == ".cr3":
            row["header_status"] = "not_parsed (CR3 not supported by this script)"

        if ftype == "other":
            issue(fid, rel_posix, "unknown_file_type", "info",
                  "extension '%s' is not in the expected file types" % ext)

        row["qc_flags"] = ";".join(flags)
        rows.append(row)

    # ---- duplicates --------------------------------------------------------
    by_hash = defaultdict(list)
    for r in rows:
        if r["sha256"] and r["size_bytes"] not in ("", 0):
            by_hash[r["sha256"]].append(r)
    group_no = 0
    for h, grp in sorted(by_hash.items(), key=lambda kv: kv[1][0]["relative_path"]):
        if len(grp) < 2:
            continue
        group_no += 1
        gid = "D%04d" % group_no
        locations = {(r["drillhole_id"], r["box_number"]) for r in grp}
        across = len(locations) > 1
        for r in grp:
            r["duplicate_group"] = gid
            r["duplicate_count"] = len(grp)
            others = [o["relative_path"] for o in grp if o is not r]
            flag = "duplicate_across_boxes" if across else "exact_duplicate"
            r["qc_flags"] = ";".join(x for x in [r["qc_flags"], flag] if x)
            issue(r["file_id"], r["relative_path"], flag,
                  "error" if across else "warning",
                  "identical content to: %s" % " | ".join(others))

    # same file name, different content (e.g. camera counter restarted)
    by_name = defaultdict(list)
    for r in rows:
        if r["file_type"] in IMAGE_TYPES:
            by_name[r["filename"].lower()].append(r)
    for name, grp in by_name.items():
        if len({r["sha256"] for r in grp}) > 1:
            for r in grp:
                others = [o["relative_path"] for o in grp if o is not r]
                issue(r["file_id"], r["relative_path"], "same_name_different_content", "info",
                      "same file name, different content: %s" % " | ".join(others))

    # ---- per drill hole checks ---------------------------------------------
    boxes = defaultdict(set)
    journals = defaultdict(list)
    images_per_dh = defaultdict(int)
    for r in rows:
        if r["file_type"] in IMAGE_TYPES and r["drillhole_id"]:
            images_per_dh[r["drillhole_id"]] += 1
            if r["box_number"] != "":
                boxes[r["drillhole_id"]].add(int(r["box_number"]))
        if r["file_type"] == "journal" and r["drillhole_id"]:
            journals[r["drillhole_id"]].append(r["relative_path"])

    for dh in DRILLHOLE_ALIASES:
        if images_per_dh[dh] == 0:
            issue(dh, "", "drillhole_without_images", "error",
                  "no image files were assigned to this drill hole")
        if not journals[dh]:
            issue(dh, "", "journal_missing", "warning",
                  "no journal file (.xlsm/.xlsx/.xls) was assigned to this drill hole")
        elif len(journals[dh]) > 1:
            issue(dh, "", "multiple_journals", "info",
                  "several journal files: %s" % " | ".join(journals[dh]))
        if boxes[dh]:
            lo, hi = min(boxes[dh]), max(boxes[dh])
            missing = [n for n in range(lo, hi + 1) if n not in boxes[dh]]
            if missing:
                unresolved = sum(1 for r in rows if r["drillhole_id"] == dh
                                 and r["file_type"] in IMAGE_TYPES and r["box_number"] == "")
                note = (" (%d photo(s) of this drill hole have no resolved box number"
                        " and may belong to these boxes)" % unresolved) if unresolved else ""
                issue(dh, "", "missing_box_numbers", "warning",
                      "box numbers %d-%d found; no photos for box(es) %s%s"
                      % (lo, hi, ", ".join(ranges(missing)), note))

    # ---- compare with previous run -----------------------------------------
    inv_path = meta_dir / "source_inventory.csv"
    verification = []
    if inv_path.exists():
        hist = meta_dir / "history"
        hist.mkdir(exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = hist / ("source_inventory_%s.csv" % stamp)
        k = 1
        while archived.exists():
            k += 1
            archived = hist / ("source_inventory_%s_%d.csv" % (stamp, k))
        shutil.copy2(str(inv_path), str(archived))
        if not args.no_verify:
            prev = read_previous_inventory(inv_path)
            cur = {r["relative_path"]: r for r in rows}
            for rel in sorted(set(prev) | set(cur)):
                p_r, c_r = prev.get(rel), cur.get(rel)
                if p_r and not c_r:
                    status = "missing_now"
                    issue(p_r.get("file_id", ""), rel, "file_missing_since_last_run", "error",
                          "file was in the previous inventory but is gone now")
                elif c_r and not p_r:
                    status = "new"
                    issue(c_r["file_id"], rel, "new_file_since_last_run", "info",
                          "file was not in the previous inventory")
                elif p_r["sha256"] != c_r["sha256"]:
                    status = "CHANGED"
                    issue(c_r["file_id"], rel, "file_changed_since_last_run", "error",
                          "checksum differs from the previous inventory")
                else:
                    status = "unchanged"
                verification.append({
                    "relative_path": rel, "status": status,
                    "previous_sha256": p_r["sha256"] if p_r else "",
                    "current_sha256": c_r["sha256"] if c_r else "",
                })
            write_csv(qc_dir / "checksum_verification.csv",
                      ["relative_path", "status", "previous_sha256", "current_sha256"],
                      verification)

    # ---- write outputs -----------------------------------------------------
    sev_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: (sev_order.get(x["severity"], 9), x["issue_type"], x["relative_path"]))
    for n, it in enumerate(issues, 1):
        it["issue_id"] = "QC%05d" % n

    write_csv(inv_path, INVENTORY_COLUMNS, rows)
    write_csv(qc_dir / "validation_report.csv", REPORT_COLUMNS, issues)
    with open(meta_dir / "sha256sums.txt", "w", encoding="utf-8") as f:
        for r in rows:
            if r["sha256"]:
                f.write("%s *%s\n" % (r["sha256"], r["relative_path"]))

    counts = defaultdict(int)
    for it in issues:
        counts[it["severity"]] += 1
    run_info = {
        "script": "inventory.py", "script_version": SCRIPT_VERSION,
        "python_version": platform.python_version(), "platform": platform.platform(),
        "started": started.isoformat(timespec="seconds"),
        "finished": dt.datetime.now().isoformat(timespec="seconds"),
        "source_folder": str(src.resolve()),
        "files_inventoried": len(rows), "system_files_skipped": skipped,
        "total_bytes": total_bytes,
        "issues": dict(counts),
        "config": {
            "drillhole_aliases": DRILLHOLE_ALIASES, "box_patterns": BOX_PATTERNS,
            "bare_number_folders_are_boxes": args.bare_number_folders_are_boxes,
        },
    }
    with open(meta_dir / "inventory_run_info.json", "w", encoding="utf-8") as f:
        json.dump(run_info, f, ensure_ascii=False, indent=2)

    # ---- summary -----------------------------------------------------------
    by_type = defaultdict(int)
    for r in rows:
        by_type[r["file_type"]] += 1
    print("\nDone. %d files, %.2f GB" % (len(rows), total_bytes / 1e9))
    for t in sorted(by_type):
        print("  %-20s %d" % (t, by_type[t]))
    print("Issues: %d errors, %d warnings, %d info"
          % (counts["error"], counts["warning"], counts["info"]))
    if verification:
        changed = sum(1 for v in verification if v["status"] in ("CHANGED", "missing_now"))
        print("Checksum comparison with previous run: %d changed/missing" % changed)
    print("Results in: %s" % out.resolve())


if __name__ == "__main__":
    main()
