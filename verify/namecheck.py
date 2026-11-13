"""Fail on a name a shipped module uses but never binds.

    python3 verify/namecheck.py                     # the *.py at the repo root (what ships)
    python3 verify/namecheck.py dist/<id>-<ver>.zip # the *.py inside a built package

Why this exists: `10586b6` deleted `_find_header` and left four call sites
(N-panel draw, dataref picker, "Animate Part", "Convert selection").  Every gate
here passed for six days - the three green tests never touch those paths, and
`bundle_smoke.py` imports through `import_scene.xplane_obj`, which hands
`_convert()` a header it already has.  The extension reviewer found it instead:
"Lots of syntax errors for _find_header that is not declared. Please make sure
to check the code in IDE."

Compiling does not catch this; only resolution does.  `symtable` resolves it
exactly, with no dependency to install: a symbol that a scope marks `is_global()`
or `is_free()`-less and references, that no module-level binding provides, and
that is not a builtin, is a NameError waiting for the user who clicks that
button.

Aliases Blender itself injects into an add-on's namespace (`bpy_types`) are
resolved, because a package can import them as `_bpy` and still see the plain
name at runtime (`__builtins__` is extended by `bpy`).  Keep that list short and
justified.
"""
import ast
import builtins
import os
import sys
import symtable
import zipfile

# Names Blender's own bootstrap puts in a module's globals.
INJECTED = {
    "bpy_types", "bpy_restrict_state", "_bpy", "bpy_ops_submodule",
}

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _module_bindings(table):
    out = set()
    for sym in table.get_symbols():
        if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
            out.add(sym.get_name())
    return out


def _walk(table, out, where):
    builtin = set(dir(builtins)) | INJECTED
    for sym in table.get_symbols():
        if not sym.is_referenced() or sym.is_parameter():
            continue
        if sym.is_local() or sym.is_free() or sym.is_imported() or sym.is_assigned():
            continue
        name = sym.get_name()
        if name.startswith("__") and name.endswith("__"):
            continue
        if sym.is_global() and name not in out and name not in builtin:
            # Symbol has no line; its scope does - enough to tell two
            # `execute()`s apart in the report.
            line = table.get_lineno() if hasattr(table, "get_lineno") else 0
            yield where, name, line
    for child in table.get_children():
        yield from _walk(child, out, child.get_name())


def undefined(src, filename):
    """[(scope, name)] - every global a scope uses that the module never binds."""
    try:
        st = symtable.symtable(src, filename, "exec")
    except SyntaxError as exc:
        return [("<module>", "SyntaxError: %s" % exc)]
    bound = _module_bindings(st)
    seen, hits = set(), []
    for scope, name, line in _walk(st, bound, "<module>"):
        if (scope, name, line) in seen:
            continue
        seen.add((scope, name, line))
        hits.append((scope, name, line))
    # a class body resolves through the module too
    return hits


def py_files(target):
    if target.endswith(".zip"):
        with zipfile.ZipFile(target) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith(".py") and "/" not in name:
                    yield name, zf.read(name).decode("utf-8", "replace")
        return
    root = target if os.path.isdir(target) else REPO
    for name in sorted(os.listdir(root)):
        if name.endswith(".py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                yield name, fh.read()


def main(argv):
    target = argv[1] if len(argv) > 1 else REPO
    bad = 0
    for name, src in py_files(target):
        for scope, missing, line in undefined(src, name):
            print("%s:%s in %s() uses undefined name %r"
                  % (name, line or "?", scope, missing))
            bad += 1
    if bad:
        print("RESULT: FAIL %d undefined name(s)" % bad)
        return 1
    print("RESULT: PASS no undefined names")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
