# Arch Update Deck

A command-line tool for updating Arch Linux. It has an interactive menu, a
set of read-only inspection commands, an optional daily read-only patrol, and
an unattended weekly update that checks the machine before and after it
changes anything.

It is for someone who administers their own Arch machine and wants the weekly
update of pacman, Flatpak and Snap (and the AUR, if they turn that on) to run
without them. Failures that have happened before are handled in a fixed way;
every other failure is reported with a command to run by hand. It does not read
the Arch news, and it does not replace knowing how to repair a system that no
longer boots.

## Requirements

- Python 3.11 or later (standard library only) and `pacman`.
- For the unattended update: `sudo` and `visudo` (not part of a base install),
  `tar` with `zstd`, `pacman-contrib` (`checkupdates`, which also needs
  `fakeroot`, plus `paccache` and `pacdiff`), and a systemd user session. For
  the weekly run to happen while you are logged out, also lingering
  (`loginctl enable-linger`).
- Optional: `yay` (AUR; `paru` works only inside `auto`), `flatpak`, `snap`,
  `libnotify` (desktop notices), `arch-audit` (CVE report), `fzf` and `gum`
  (menu), a terminal the tool recognises (ask mode, click-to-fix), `script`,
  `fuser`, `lsof`, `resolvectl`.

The reference lists, for each program, what is lost without it.

## Status

Version 0.17.0. Written for one machine first; tested on Arch with systemd; expect rough edges. Issues welcome.

## Installation

```bash
git clone https://github.com/zzallirog/arch-update-deck && cd arch-update-deck && ./install.sh
command -v arch-update
```

The installer needs no root. It replaces the copy of the sources in
`~/.local/share/arch-update-deck/`, puts the launcher in
`~/.local/bin/arch-update` and a `.desktop` file in
`~/.local/share/applications/`. Set `ARCH_UPDATER_PREFIX` to install
elsewhere. Run it again after every update of the source tree.
`pyproject.toml` also declares the package for `pip install .`; see the
reference.

The installer prints a warning if `~/.local/bin` is not in `PATH`. If
`command -v arch-update` prints nothing, add it:

```bash
# bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
# fish
fish_add_path ~/.local/bin
```

## First run

[START-HERE.md](START-HERE.md) walks through it in order: create the
configuration, choose a record directory, allow the update commands in sudoers,
look at the machine with `arch-update auto --dry-run`, then enable the timer.
It ends with a way to watch a repair on a live system.

## What it does and does not do

- **It never reboots.** No code path runs `reboot`, `shutdown` or `poweroff`. A
  pending reboot is reported (exit status 10, `REBOOT NEEDED`), and
  `auto --status` prints `systemctl reboot` as text for you to run.
- **It never merges a `.pacnew` file.** They are found with `pacdiff -o`
  (`engine._pacnew_files`), which only lists them; the menu shows a `diff -u`
  (`ui._pacnew_menu`). No code writes to them.
- **It never removes the pacman lock** `/var/lib/pacman/db.lck`. The only
  operation on it is `Path.exists()` (`watch.probe_lock`,
  `repairs.wait_for_lock`, `engine._preflight`). The interactive update refuses
  to start while it exists. The unattended update waits for it up to three
  times for two minutes and then exits 30.
- **The sudoers rules are exact commands.** `auto --sudoers` prints one rule per
  command with its arguments fixed (`auto.sudoers_rules`). Each program is
  looked up only in `/usr/bin`, `/usr/sbin`, `/bin` and `/sbin`, never in your
  `PATH`, and only a file owned by root that group and others cannot write gets
  a rule (`Shell.root_binary`); otherwise the output has a `# skipped` line. The
  one exception is `--aur`: see [AUR](#aur).
- Package and boot-file transactions (`pacman`, `yay`, `paru`, `paccache`,
  `mkinitcpio`, `dkms`, `snap`, `flatpak`, `grub-mkconfig`) never get a time
  limit (`shell.TRANSACTION_TOOLS`). The checks and the `tar` of `/etc` do.
- The unattended update runs `pacman -Syu --noconfirm` without looking at the
  Arch news. A conflict that pacman cannot settle that way makes the store fail
  and is reported.
- It takes no shutdown or sleep inhibitor. A logout, suspend or power-off during
  an update is not guarded.
- It has no command to restore the record it writes before an update.
- Free space on `/`: the unattended update does not start below 6 GiB and tries
  `sudo paccache -rk2` first. The interactive update below 6 GiB runs
  `sudo paccache -rk1` (asking in the menu, not asking in `arch-update run`) and
  stops below 4 GiB. Both cleanups delete cached package files, keeping the
  newest two versions (unattended) or one (interactive) of each package.
- Kernel selection installs a kernel package and, with GRUB, rewrites
  `GRUB_DEFAULT`. It needs a confirmation or `--yes`. It backs up
  `/etc/default/grub` first and restores the backup if writing or regenerating
  `grub.cfg` fails. The backups are never cleaned up.
- Some files grow without limit: `runs/`, `events.jsonl`, the logs, the grub
  backups and the quarantined AUR caches. The reference lists them.

## Usage

| Command | Action |
|---|---|
| `arch-update`, `arch-update menu` | Open the interactive menu. |
| `arch-update status [--json]` | Show queue, kernel, disk, failed units, `.pacnew` files. |
| `arch-update plan [--mode M] [--kernel ID]` | Print the commands an update would run (`M`: `full`, `repo`, `aur`, `attest`). Changes nothing. |
| `arch-update run [--mode M] [--dry-run] [--yes]` | One interactive update. `--yes` passes `--noconfirm`. `full` and `aur` need `yay`. Exits 1 if a command failed. |
| `arch-update attest` | Check kernel, initramfs, DKMS, failed units and reboot need. |
| `arch-update kernel [profile] [--dry-run] [--yes]` | List kernel profiles, or install and select one. |
| `arch-update vault [--classify TEXT]` | Print the catalogue of known failures, or the entries matching `TEXT`. |
| `arch-update cve` | Report vulnerable packages with `arch-audit`. |
| `arch-update schedule [--install] [--install-auto] [--remove] [--remove-auto]` | Show the timers, or install or remove the daily patrol and the weekly update. |
| `arch-update auto [--dry-run] [--status] [--init] [--ask] [--sudoers [--aur]]` | The unattended update. |
| `arch-update patrol` | Run the patrol now (what the daily timer runs). |
| `arch-update share-report` | Print a sanitised summary of the last patrol as JSON. Run `patrol` first. Sends nothing. |
| `arch-update scan-sessions [--output-dir DIR] [--json]` | See [Session scanner](#session-scanner). |

The menu opens in `fzf` (or `gum`, or a numbered list). Repos, AUR, Full and
Kernel each ask once in Deck (Enter = no); `pacman` and `yay` then show their
own prompts, where Enter = yes, unless you pass `run --yes` (`--noconfirm`). A
low-space cache cleanup asks one extra question. Menu and `run` use `yay` for
the AUR.

## Unattended update

`arch-update auto` updates every installed store: `pacman`, `yay` (or `paru`),
`flatpak`, `snap`. It reads no queue and shows no diffs.

1. **Check.** Nine checks run concurrently and change nothing: root access, the
   configured conditions, the record directory, the pacman lock, disk space,
   name resolution, the package database, kernel and services, the configured
   programs.
2. **Update.** Each store is updated once, after a record of the package lists
   and the readable part of `/etc` is written. A failing store does not stop the
   others.
3. **Repair.** A failure is looked up in the catalogue of known failures. If a
   repair is recorded for it, the repair runs and the sequence returns to
   step 1. At most three repairs run per update.
4. The run ends when the checks pass and no store is left to update, or when a
   failure has no recorded repair or the repairs are spent, or when a check is
   blocked.

A failed store whose failure is new is added to
`~/.local/state/arch-updater/census.json` with the line that identified it and
`less +G <run.log>` as its `fix`. `arch-update auto --status` prints what is
still wrong, each with its `fix`; a desktop notification (if `notify-send`
exists) says the same, and clicking it opens a terminal on the `fix`.

Exit status: 0 idle, 10 idle with a reboot pending, 20 blocked, 30 a failure is
left, 40 and 50 a condition or the record directory said no, 70 setup or
internal error, 80 skipped (another run holds the lock, or the ask window got
"not now" or timed out; the update did not happen). The reference says what a
script should do with each.

### AUR

With `"unattended_aur": false` (the default) the weekly update skips the AUR
and reports it as held. Update it by hand with `arch-update run --mode aur`.

With `"unattended_aur": true`:

- `yay -Sua --noconfirm --answerclean None --answerdiff None --answeredit None`
  builds the packages. Nobody reads the `PKGBUILD` or the diff before it runs,
  and a build script runs as your user. With `paru` the flags are
  `--noconfirm --skipreview`.
- Before building, the names of all your foreign packages (`pacman -Qqm`) are
  sent to `https://aur.archlinux.org/rpc/v5/info`. Packages whose AUR entry
  changed within `aur_min_age_days` (default 4) are skipped. This is a
  cool-down, not a check of the package.
- Installing a build without a password needs the sudoers rules printed by
  `arch-update auto --sudoers --aur`. They are written for `yay` only.

Warning: those four rules end in a sudoers `*`. They are not limited to one
package: they allow `pacman -U` on files in the user's `yay` cache and
`pacman -D` and `pacman -S` on any package name. Any process running as the
user can then install an arbitrary package as root.

### Root access

The timer runs as the user, so the commands it runs through `sudo -n` must be
allowed without a password. Print the rules, read them, then install them:

```bash
arch-update auto --sudoers
sh -c 'f=$(mktemp) && arch-update auto --sudoers > "$f" && visudo -cf "$f" && sudo install -m 0440 "$f" /etc/sudoers.d/zzz-arch-update-auto; rm -f "$f"'
```

The second line works in bash and fish and stops if `visudo` finds an error.
Keep the file name: sudo applies the last matching rule, and this file must sort
after the rule that grants you all commands with a password.

### Schedule

```bash
arch-update schedule --install-auto
```

Writes `arch-update-auto.service` and `.timer` to `~/.config/systemd/user/` and
enables the timer: Saturday 12:00, up to 30 minutes of random delay,
`Persistent=true`. After a missed run, it starts when your user manager next
starts. The service has `SuccessExitStatus=10 80`; any other status leaves the
unit failed.

With `"ask": true` in the configuration the timer opens a terminal on
`arch-update auto --ask`, which lists what is waiting and asks. `y` updates;
anything else does nothing. No answer within `ask_timeout_minutes` (default 30)
skips the week. Ask mode needs a recognised terminal and `DISPLAY` or
`WAYLAND_DISPLAY` in the systemd user manager's environment; the reference has
the list of terminals, the keys and the exit statuses.

### Daily patrol

`arch-update schedule --install` enables a daily read-only job (09:30, up to 15
minutes of delay). It runs `pacman -Q`, `systemctl --failed`, `pacdiff -o`,
`checkupdates`, `yay -Qua`, `arch-audit` and a few more read-only queries,
each only if installed. It never installs anything and never uses `sudo`.
`checkupdates` syncs a temporary copy of the package databases, and `yay -Qua`
and `arch-audit` use the network. The result goes to `last-patrol.json`.

### Session scanner

`arch-update scan-sessions` reads local Codex session files
(`~/.codex/sessions/`), Claude Code session files for your home directory
(`~/.claude/projects/`) and `/var/log/pacman.log`, to find sessions in which a
package-manager command changed the system. It runs only when you ask, and the
menu asks first. It writes three report files to `reports/` in the state
directory (or to `--output-dir`), with secrets in commands masked. Nothing
leaves the machine; the scanner makes no network call.

## Removing it

There is no uninstall command for the files. Stop and delete both timers,
remove the sudoers file, then the installed copies:

```bash
arch-update schedule --remove
arch-update schedule --remove-auto
sudo rm -f /etc/sudoers.d/zzz-arch-update-auto
rm -rf ~/.local/share/arch-update-deck ~/.local/bin/arch-update ~/.local/share/applications/arch-update.desktop
```

The configuration (`~/.config/arch-updater/`), the state directory
(`~/.local/state/arch-updater/`), the GRUB backups and the yay quarantine
directories stay until you delete them.

## Reference

[docs/REFERENCE.md](docs/REFERENCE.md) has everything else: what each optional
program is for, the sequence and the checks in detail, the catalogue and the
repair names, ask mode, the exit-status table, every configuration key, every
file the tool creates, the environment variables, `pip install`, and testing.

## License

MIT. See [LICENSE](LICENSE).
