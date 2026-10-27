#!/bin/sh
# Symlink this repository into Blender's user extension folder, so editing the
# files here is live in Blender -- no reinstall, no zip, just "Reload Scripts".
#
#   ./dev-link.sh            # every installed Blender version
#   ./dev-link.sh 5.2        # one version only
#
# The addon carries a blender_manifest.toml, so it belongs in an *extension*
# repository, not scripts/addons/: there it is listed in Preferences > Add-ons
# (and can be ticked), registers as bl_ext.user_default.io_scene_xplane_obj,
# and Blender does not warn about a missing legacy bl_info dict.
set -e

ID=io_scene_xplane_obj
REPO=$(cd "$(dirname "$0")" && pwd)

case "$(uname -s)" in
    Darwin) BASE="$HOME/Library/Application Support/Blender" ;;
    Linux)  BASE="$HOME/.config/blender" ;;
    *)      echo "unsupported platform: $(uname -s)." >&2
            echo "On Windows, run from an elevated shell:" >&2
            echo "  mklink /D \"%APPDATA%\\Blender Foundation\\Blender\\<ver>\\extensions\\user_default\\$ID\" \"$REPO\"" >&2
            exit 1 ;;
esac

link_one() {
    ver="$1"
    repo_dir="$BASE/$ver/extensions/user_default"
    [ -d "$BASE/$ver" ] || return 0
    mkdir -p "$repo_dir"
    target="$repo_dir/$ID"
    if [ -L "$target" ]; then
        current=$(readlink "$target")
        if [ "$current" = "$REPO" ]; then
            echo "  $ver: already linked"
            return 0
        fi
        echo "  $ver: relinking (was $current)"
        rm -f "$target"
    elif [ -e "$target" ]; then
        echo "  $ver: $target exists and is not a symlink -- left alone" >&2
        return 0
    fi
    ln -s "$REPO" "$target"
    echo "  $ver: $target -> $REPO"
}

if [ -n "$1" ]; then
    link_one "$1"
else
    for d in "$BASE"/*/; do
        [ -d "$d/extensions" ] || continue
        link_one "$(basename "$d")"
    done
fi

echo
echo "Enable it in Blender: Edit > Preferences > Add-ons > X-Plane OBJ8 Object"
echo "or headless:  bpy.ops.preferences.addon_enable(module=\"bl_ext.user_default.$ID\")"
echo "After editing the source: F3 > Reload Scripts (Blender restarts the addon)."
