"""Materialise `xplane2blender/` from the vendored submodule, with its imports fixed.

The exporter lives in its own repository (`vendor/XPlane2Blender`, pinned in
`.gitmodules`) so its history stays where it belongs. What this repository ships
is a *generated* copy whose imports work when the package sits one level down
inside this extension - so it is never edited by hand:

    python3 scripts/vendor_bundle.py

Two transformations, and nothing else:

1. `from io_xplane2blender.a.b import x` -> `from .a.b import x` (`..` one
   directory deeper). A package cannot be reached by its old top-level name from
   inside another package.
2. The handful of places that did `import io_xplane2blender` and then walked its
   attributes name the module they use instead, through `FIXES` below - exact
   upstream snippets, because the import has to land in the right scope (two of
   them are circular at module level and belong inside the function).

No `sys.modules` alias for the old package name: that loads every submodule twice
and a session that also has a standalone XPlane2Blender enabled ends up with two
counters, two updaters and two registration passes.

Anything that still says `io_xplane2blender` afterwards aborts the run with
file:line, so an upstream edit cannot silently ship a package that fails at
import time. `verify/bundle_smoke.py` then grades the built zip.
"""
import io
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "vendor", "XPlane2Blender", "io_xplane2blender")
DST = os.path.join(REPO, "xplane2blender")

# (file, exact upstream text, replacement). Snippets rather than regexes: if
# upstream changes the line, the leftovers check below names it instead of the
# transform silently doing half a job.
FIXES = [
    ("xplane_helpers.py",
     "import io_xplane2blender\n",
     ""),
    # the counter is a module global upstream reached through the package name;
    # a bare name in an assignment makes it a *local*, so say `global` or every
    # log message dies with UnboundLocalError
    ("xplane_helpers.py",
     "        io_xplane2blender.xplane_helpers.message_to_str_count += 1",
     "        global message_to_str_count\n"
     "        message_to_str_count += 1"),
    ("xplane_helpers.py",
     "if io_xplane2blender.xplane_helpers.message_to_str_count == 1:",
     "if message_to_str_count == 1:"),
    ("xplane_ops_dev.py",
     "import io_xplane2blender\n",
     ""),
    ("xplane_ops_dev.py",
     "        io_xplane2blender.xplane_updater.update(",
     "        from . import xplane_updater\n"
     "        xplane_updater.update("),
    ("xplane_props.py",
     "import io_xplane2blender\n",
     ""),
    ("xplane_export.py",
     "import io_xplane2blender\n",
     ""),
    ("xplane_export.py",
     "                logger.error(\n"
     "                    \"Save your blend file before using the '%s' button\"\n"
     "                    % io_xplane2blender.xplane_ops."
     "SCENE_OT_export_to_relative_dir.bl_label",
     "                from . import xplane_ops  # imports this module\n"
     "                logger.error(\n"
     "                    \"Save your blend file before using the '%s' button\"\n"
     "                    % xplane_ops.SCENE_OT_export_to_relative_dir.bl_label"),
    ("xplane_types/xplane_material.py",
     "import io_xplane2blender\n",
     ""),
    ("xplane_types/xplane_material.py",
     "        import io_xplane2blender\n\n"
     "        return io_xplane2blender.xplane_types.xplane_material_utils.compare(",
     "        from . import xplane_material_utils\n\n"
     "        return xplane_material_utils.compare("),
    ("xplane_types/xplane_material.py",
     "        return io_xplane2blender.xplane_types.xplane_material_utils.validate(",
     "        from . import xplane_material_utils\n\n"
     "        return xplane_material_utils.validate("),
    ("xplane_updater.py",
     "import io_xplane2blender\n",
     ""),
    ("xplane_updater.py",
     'title="Could not read io_xplane2blender/resources/lights.txt"',
     'title="Could not read resources/lights.txt"'),
    ("xplane_updater.py",
     'title="io_xplane2blender/resources/lights.txt had invalid content"',
     'title="resources/lights.txt had invalid content"'),
    ("xplane_ui.py",
     "    import io_xplane2blender.xplane_utils.xplane_commands_txt_parser",
     "    from .xplane_utils import xplane_commands_txt_parser"),
    ("xplane_ui.py",
     "    import io_xplane2blender.xplane_utils.xplane_datarefs_txt_parser",
     "    from .xplane_utils import xplane_datarefs_txt_parser"),
    # upstream reads context.active_object without a guard, so an empty scene
    # (no active object) throws on every redraw of the Render panel - reported by
    # a user installing on Windows with nothing open. One `is not None`.
    ("xplane_ui.py",
     "            if context.active_object.xplane.isExportableRoot:\n"
     "                active_root = context.active_object",
     "            active = context.active_object\n"
     "            if active is not None and active.xplane.isExportableRoot:\n"
     "                active_root = active"),
    ("tests/__init__.py",
     "import io_xplane2blender\n",
     ""),
    ("tests/__init__.py",
     "            io_xplane2blender.tests.test_creation_helpers.make_root_exportable(",
     "            test_creation_helpers.make_root_exportable("),
    ("tests/test_creation_helpers.py",
     "import io_xplane2blender\n",
     ""),
    ("tests/test_creation_helpers.py",
     "    layer: Union[int, io_xplane2blender.xplane_props.XPlaneLayer],",
     "    layer: Union[int, xplane_props.XPlaneLayer],"),
    ("tests/test_creation_helpers.py",
     "        layer, io_xplane2blender.xplane_props.XPlaneLayer",
     "        layer, xplane_props.XPlaneLayer"),
]

# names those snippets now use, which the file has to import from its own package
_EXTRA_IMPORTS = {
    "tests/test_creation_helpers.py": "xplane_props",
}

SKIP_DIRS = {"__pycache__", ".git"}
SKIP_SUFFIX = (".pyc", ".pyo")


def rewrite(path, rel):
    text = io.open(path, encoding="utf-8").read()
    dots = "." * (rel.count(os.sep) + 1)

    missing = []
    for target, old, new in FIXES:
        if target != rel:
            continue
        if old not in text:
            missing.append(old.strip().splitlines()[0][:70])
            continue
        text = text.replace(old, new, 1)

    text = re.sub(r"from io_xplane2blender\.(?=\w)", "from %s" % dots, text)
    text = re.sub(r"from io_xplane2blender import", "from %s import" % dots, text)

    extra = _EXTRA_IMPORTS.get(rel)
    if extra and not re.search(r"^from %s import .*\b%s\b"
                               % (re.escape(dots), extra), text, re.M):
        text = re.sub(r"^(from io_xplane2blender import )",
                      r"from %s import " % dots, text, count=1, flags=re.M)
        text = re.sub(r"^(from %s import [^\n]*)$" % re.escape(dots),
                      r"\1, %s" % extra, text, count=1, flags=re.M)

    io.open(path, "w", encoding="utf-8").write(text)
    return missing


def main():
    if not os.path.isdir(SRC):
        sys.exit("no submodule at %s\nrun: git submodule update --init" % SRC)
    if os.path.isdir(DST):
        shutil.rmtree(DST)
    shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    for dirpath, _dirs, files in os.walk(DST):
        for name in files:
            if name.endswith(SKIP_SUFFIX):
                os.remove(os.path.join(dirpath, name))

    rewritten = 0
    unmatched = []
    for dirpath, _dirs, files in os.walk(DST):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, DST)
            before = io.open(path, encoding="utf-8").read()
            for missed in rewrite(path, rel):
                unmatched.append("%s: %r" % (rel, missed))
            if io.open(path, encoding="utf-8").read() != before:
                rewritten += 1

    leftovers = []
    for dirpath, _dirs, files in os.walk(DST):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            for lineno, line in enumerate(io.open(path, encoding="utf-8"), 1):
                if "io_xplane2blender" in line:
                    leftovers.append("%s:%d: %s"
                                     % (os.path.relpath(path, DST), lineno,
                                        line.strip()[:100]))

    print("vendor_bundle: %d file(s) rewritten" % rewritten)
    for line in unmatched:
        print("  snippet not found: %s" % line)
    for line in leftovers:
        print("  still references the old name: %s" % line)
    if unmatched or leftovers:
        sys.exit("update FIXES in scripts/vendor_bundle.py - this package "
                 "would fail at import time")


if __name__ == "__main__":
    main()
