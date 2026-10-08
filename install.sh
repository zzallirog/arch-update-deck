#!/usr/bin/env bash
set -euo pipefail

source_dir=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
prefix=${ARCH_UPDATER_PREFIX:-"$HOME/.local"}
share_dir="$prefix/share/arch-update-deck"
bin_dir="$prefix/bin"
desktop_dir="$prefix/share/applications"

# The package is replaced as a whole, so a module that was removed upstream does not linger.
# config/ and vault/ are where earlier releases kept the data files.
rm -rf -- "$share_dir/arch_updater" "$share_dir/config" "$share_dir/vault" "$share_dir/docs"
install -d "$share_dir/arch_updater/data" "$bin_dir" "$desktop_dir"
install -m 0644 "$source_dir"/arch_updater/*.py "$source_dir"/arch_updater/*.tis "$share_dir/arch_updater/"
install -m 0644 "$source_dir"/arch_updater/data/*.json "$share_dir/arch_updater/data/"
install -d "$share_dir/docs"
install -m 0644 "$source_dir/README.md" "$source_dir/START-HERE.md" "$source_dir/LICENSE" "$share_dir/"
install -m 0644 "$source_dir/docs/REFERENCE.md" "$share_dir/docs/"
install -m 0755 "$source_dir/bin/arch-update" "$bin_dir/arch-update"

# Exec= takes a quoted string: inside the quotes \ " ` $ are escaped, and % is written %%.
exec_path=$bin_dir
exec_path=${exec_path//\\/\\\\\\\\}
exec_path=${exec_path//\"/\\\\\"}
exec_path=${exec_path//\`/\\\\\`}
exec_path=${exec_path//\$/\\\\\$}
exec_path=${exec_path//%/%%}
exec_path="$exec_path/arch-update"
shopt -u patsub_replacement 2>/dev/null || true

desktop_tmp=$(mktemp)
trap 'rm -f -- "$desktop_tmp"' EXIT
while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "${line//@EXEC@/$exec_path}"
done <"$source_dir/assets/arch-update.desktop" >"$desktop_tmp"
install -m 0644 "$desktop_tmp" "$desktop_dir/arch-update.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

printf 'Installed: %s\n' "$bin_dir/arch-update"
printf 'Desktop:   %s\n' "$desktop_dir/arch-update.desktop"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) printf 'Warning: %s is not in your PATH, so `arch-update` will not be found.\n         Add it, for example: export PATH="%s:$PATH"\n' "$bin_dir" "$bin_dir" >&2 ;;
esac
"$bin_dir/arch-update" --version
