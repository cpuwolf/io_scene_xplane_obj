"""The .obj files an X-Plane aircraft (.acf) attaches.

X-Plane 10 and later writes the aircraft out as text: one
``<kind> <key> <value>`` line per property, and every attached object is a
property of the form

    P _obja/<slot>/_v10_att_file_stl <path>

The path is not always relative to the folder the .acf sits in.  X-Plane looks
for the aircraft's objects in the aircraft folder **and** in its ``objects/``
subfolder, and Laminar's own files rely on both:

    ../Cessna_172SP_cockpit.obj          -> <aircraft>/Cessna_172SP_cockpit.obj
    b738_cockpit.obj                     -> <aircraft>/objects/b738_cockpit.obj
    C90B_interior/C90B_cabin.obj         -> <aircraft>/objects/C90B_interior/...

so a resolver that only joins against the .acf's folder finds almost none of
them.  Order matters only when the same relative path exists in both places;
the aircraft folder wins, which is the order X-Plane itself uses.

Values are written verbatim - a file name with a space in it is *not* quoted -
so everything after the second whitespace-separated token is the path.

Read-only on purpose: this module only reads.  Laminar's own guidance for the
text format says a shipped add-on must not write .acf files, because the format
changes between sim versions and a file left behind in the field would go stale.
"""

import os

# the field name has been `_v10_att_file_stl` since the text format arrived in
# X-Plane 10 (the "stl" is historical - the file it points at is an OBJ8); XP11
# and XP12 both still write exactly this key
OBJ_FIELD = "_v10_att_file_stl"

# attached objects; `_wpna` is the same trick for weapons (.wpn) and is not ours
OBJ_SECTION = "_obja"

OBJ_EXT = ".obj"

# the two folders X-Plane resolves an attached object against, in its order
OBJECT_SUBDIR = "objects"


def read_text(path):
    """The .acf as text.  Raises ValueError for the binary (X-Plane 9) format."""
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:1024]:
        raise ValueError("binary .acf (X-Plane 9 or older): only the text "
                         "format that arrived with X-Plane 10 can be read")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # pre-XP11 files were written in the system code page; the paths we
        # want are ASCII either way, so do not fail the import over a name
        return raw.decode("latin-1")


def _value(raw):
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def slots(text):
    """[(slot, raw path)] for every attached object, in slot order.

    The file writes its properties alphabetically (``_obja/10`` before
    ``_obja/2``), while the slot number is the order the objects were attached
    in Plane-Maker, so sort numerically - that is the order worth importing in.
    """
    found = []
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line or line[0] in "#;":
            continue
        words = line.split(None, 2)
        if len(words) < 3:
            continue
        parts = words[1].split("/")
        if len(parts) != 3 or parts[0] != OBJ_SECTION or parts[2] != OBJ_FIELD:
            continue
        if not parts[1].isdigit():
            continue
        found.append((int(parts[1]), _value(words[2])))
    found.sort(key=lambda pair: pair[0])
    return found


def resolve(acf_path, text):
    """(existing .obj paths, raw names not found), deduplicated, slot order.

    A file may attach the same .obj to more than one slot (the C90B cabin is
    slot 0 *and* slot 1); importing it twice would build two identical
    collections, so the second occurrence is dropped, keeping the lowest slot.
    """
    aircraft = os.path.dirname(os.path.abspath(acf_path))
    bases = [aircraft, os.path.join(aircraft, OBJECT_SUBDIR)]
    seen = set()
    reported = set()
    paths = []
    missing = []
    for _slot, raw in slots(text):
        name = raw.replace("\\", "/")
        if not name.lower().endswith(OBJ_EXT):
            continue
        candidates = [os.path.normpath(os.path.join(base, name))
                      for base in bases]
        hit = next((c for c in candidates if os.path.isfile(c)), None)
        if hit is None:
            key = os.path.normcase(candidates[0])
            if key not in reported:
                reported.add(key)
                missing.append(raw)
            continue
        key = os.path.normcase(hit)
        if key in seen:
            continue
        seen.add(key)
        paths.append(hit)
    return paths, missing


def objects(acf_path):
    """(existing .obj paths, raw names missing on disk) for one .acf."""
    return resolve(acf_path, read_text(acf_path))
