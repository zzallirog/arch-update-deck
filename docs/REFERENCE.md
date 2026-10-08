# Reference

Details for [README.md](../README.md). Nothing here is needed for a first run; see
[START-HERE.md](../START-HERE.md) for that.

## Requirements

By what each program is used for.

| Needed for | Program | Without it |
|---|---|---|
| Everything | Python 3.11 or later (standard library only), `pacman` | Does not run. |
| Unattended update | `sudo` and `visudo` (package `sudo`; not part of a base install) | No passwordless root, so `auto` exits 70. |
| Unattended update | `tar` with `zstd` | The record of `/etc` cannot be written, so the update does not start (exit 50). |
| Unattended update, patrol, `status` | `checkupdates` (package `pacman-contrib`; it also needs `fakeroot`) | The repository queue is shown as unknown (`?`, with the note "install pacman-contrib"). The update itself still runs. |
| Low-space repair, cleanup | `paccache` (`pacman-contrib`) | No cleanup. The sudoers rule for it is not generated. |
| `.pacnew` listing | `pacdiff` (`pacman-contrib`) | No `.pacnew` files are listed. |
| Timers | `systemd --user` | `schedule` cannot install the timers. |
| Weekly run while logged out | Lingering: `loginctl enable-linger` | The user manager, and with it the timer, runs only while you are logged in. |
| AUR | `yay`, or `paru` for `auto` only | No AUR updates. See [AUR](../README.md#aur). |
| Flatpak, Snap | `flatpak`, `snap` | Those stores are skipped. |
| Desktop notice | `notify-send` (package `libnotify`) | No notification is sent. |
| Click-to-fix | `systemd-run`, a recognised terminal (see [Schedule and ask mode](#schedule-and-ask-mode)), and a `notify-send` that supports `--wait` and `--action` | The notice has no click action. |
| Ask mode | A recognised terminal, and a graphical session known to the systemd user manager | The weekly run updates without asking. |
| CVE report | `arch-audit` | The report says the CVE state is unknown. |
| Menu | `fzf`, `gum` | Without `fzf`, `gum` is used. Without both, the menu is a numbered list. |
| Logging with a terminal | `script` (`util-linux`) | `run` and the menu pipe the package manager's output through Python instead of giving it a terminal. |
| Lock report | `fuser`, `lsof` | The lock report omits their output. |
| DNS repair | `resolvectl` | The cache flush fails; the repair still waits for name resolution. |

## Free space on `/`

- Unattended update: the disk check fails below 6 GiB free
  (`SOFT_FREE_BYTES` in `arch_updater/engine.py`, `probe_disk` in
  `arch_updater/watch.py`). The repair for it runs
  `sudo paccache -rk2`, which deletes every cached package file except the
  two newest versions of each package, and then checks again. If space is
  still low after three repairs, the run exits 30.
- Interactive update (`arch-update run`, and Repos, AUR, Full and Engine in
  the menu): below 6 GiB the cleanup `sudo paccache -rk1` runs first. It
  deletes every cached package file except the newest version of each
  package. In the menu you are asked to confirm it. `arch-update run` has no
  prompt, so it runs the cleanup without asking. If less than 4 GiB is free
  after that, the update stops (`HARD_FREE_BYTES`). The 4 GiB floor also
  applies to `--dry-run` and `--mode attest`, which never clean the cache.
- The patrol does not check free space.

## Commands in detail

| Command | Detail |
|---|---|
| `status [--json]` | Shows the package queue, kernel, disk, failed units and `.pacnew` files. `--json` prints the raw snapshot. Runs `checkupdates` and `yay -Qua`. |
| `plan [--mode full\|repo\|aur\|attest] [--kernel ID]` | Prints as JSON the commands an update would run, the detected kernel, the lock state, free space and the queue. Runs nothing that changes the system. |
| `run [--mode full\|repo\|aur\|attest] [--dry-run] [--yes]` | Runs one update at the terminal: checks, then `sudo pacman -Syu`, then `yay -Sua`, then a health check. `full` and `aur` need `yay`; without it they stop before running anything. Exits 1 if a command failed. `--dry-run` still performs the checks and the final health check and prints the update commands instead of running them; it writes a `runs/<id>/` directory and `last-run.json` like a real run. |
| `attest` | Prints as JSON whether kernel, initramfs, DKMS, failed units and boot loader entry are in order, and whether a reboot is pending. |
| `kernel [profile] [--dry-run] [--yes]` | Without a profile, lists kernel profiles. With one, asks for confirmation (at a terminal; without one, `--yes` is required), then installs the kernel and selects it. `--dry-run` prints what would change and does not ask. `auto` keeps the detected kernel. |
| `vault [--classify TEXT]` | Prints the shipped catalogue of known failures, or only the entries whose pattern matches `TEXT`. |
| `schedule [--install] [--install-auto] [--remove] [--remove-auto]` | Without flags, shows the timers. `--remove` and `--remove-auto` stop one timer and delete its unit files. |
| `patrol` | Runs the patrol now and prints its report. |
| `share-report` | Prints a sanitised summary of the last patrol as JSON (schema `arch-update-share/v1`). It writes nothing and sends nothing. Without a patrol report it exits 1. |
| `scan-sessions [--output-dir DIR] [--json]` | See [Session scanner](#session-scanner). |

`run` passes `--noconfirm` only with `--yes`. `auto` passes `--noconfirm` to
`pacman`, `yay` and `paru`, and `-y --noninteractive` to `flatpak`.

`preview`, `preview-pkg`, `preview-pick`, `snapshot-json`, `search` and
`store-search` are helpers the menu calls for its preview panes.

Other commands exit 0 on success and 1 on an error or a refusal (2 for a usage
error); `run` also exits 1 if one of its commands failed.

## Safety statements in detail

- No reboot: no code path runs `reboot`, `shutdown` or `poweroff`. A pending
  reboot means the module directory of the running kernel is gone
  (`engine.detect_kernel`, `watch.probe_machine`); the run then exits 10.
- `.pacnew` files are listed with `pacdiff -o`. The menu shows
  `diff -u <file> <file>.pacnew` and overwrites nothing.
- The pacman lock is only tested with `Path.exists()`. `fuser -v` and `lsof`
  are run on it for the lock report in the menu, which only prints their output.
- The unattended update runs `pacman -Syu --noconfirm` and does not fetch the
  Arch news (the menu's News entry does, separately, from
  `https://archlinux.org/feeds/news/`). A conflict that pacman cannot settle
  with `--noconfirm` makes the store fail (for example
  `PACMAN-REPLACEMENT-CONFLICT`) and is reported.
- No shutdown or sleep inhibitor is taken. A logout, suspend or power-off during
  an update is not guarded. SIGTERM lets the command in progress finish.
- The record written before an update has no restore command.
- A pacman lock left by a killed package manager is waited on and never
  removed; the run exits 30.
- Kernel selection installs the package with `pacman -S --needed`. With GRUB it
  rewrites `GRUB_DEFAULT`, after copying `/etc/default/grub` to
  `/etc/default/grub.arch-updater-<time>.bak`; it restores that copy if
  installing the file or regenerating `grub.cfg` fails, or if the new
  `grub.cfg` does not mention the kernel. If the backup itself fails, GRUB is
  left untouched. With another boot loader only the package is installed.
  The backups are never cleaned up.
- Failed systemd units present before an update are not attributed to it.

## AUR in detail

- With `"unattended_aur": false` (default) the weekly update skips the AUR and
  reports it under `held`. Update it with `arch-update run --mode aur`, or
  click the notification.
- With `true`, `yay -Sua --noconfirm --answerclean None --answerdiff None
  --answeredit None --sudoflags -n` builds the packages (`paru`:
  `-Sua --noconfirm --skipreview --sudoflags -n`). Nobody reads the `PKGBUILD`
  or the diff before it runs.
- The names of all foreign packages (`pacman -Qqm`) are sent to the AUR RPC.
  Packages changed in the AUR within `aur_min_age_days` (default 4) are passed
  to `--ignore`. This is a cool-down that gives other people time to find and
  report a bad package; it does not check the package.
- If the age lookup fails, the AUR is not updated. That failure
  (`AUR-UNREACHABLE`) has a repair, a wait of 60 s, after which the lookup is
  tried again.
- `arch-update auto --sudoers --aur` adds four rules written for `yay` only
  (none for `paru`): `pacman -U --noconfirm --config /etc/pacman.conf --
  <cache>/*`, `pacman -D -q --asexplicit --noconfirm --config /etc/pacman.conf
  -- *`, `pacman -D -q --asdeps --noconfirm --config /etc/pacman.conf -- *`
  and `pacman -S --noconfirm --config /etc/pacman.conf --asdeps -- *`.
  `<cache>` is the `yay` cache under `XDG_CACHE_HOME` (default `~/.cache`) at the
  moment you print the rules. The `*` is a sudoers wildcard, so the rules are
  not limited to one package: any process running as your user can install an
  arbitrary package as root.

## Root access in detail

Without `--aur`, each rule is one exact command with its arguments fixed:
`true`, `pacman -Syu --noconfirm`, `snap refresh`, `paccache -rk2`,
`mkinitcpio -P`, `dkms autoinstall`. The program in each rule is looked up only
in `/usr/bin`, `/usr/sbin`, `/bin` and `/sbin`, never in your `PATH`, and only a
file that root owns and that group and others cannot write gets a rule. For any
other program (not installed, or not meeting that test) the output has a
`# skipped <program>: <reason>` comment instead of a rule.

A full set, on a machine with `pacman`, `paccache`, `mkinitcpio` and `dkms`
(`snap refresh` is added if `snap` is installed):

```
<user> ALL=(root) NOPASSWD: /usr/bin/true
<user> ALL=(root) NOPASSWD: /usr/bin/pacman -Syu --noconfirm
<user> ALL=(root) NOPASSWD: /usr/bin/paccache -rk2
<user> ALL=(root) NOPASSWD: /usr/bin/mkinitcpio -P
<user> ALL=(root) NOPASSWD: /usr/bin/dkms autoinstall
```

The rules go in `/etc/sudoers.d/zzz-arch-update-auto`. sudo applies the last
matching rule, so the file must sort after the rule that grants the user all
commands with a password.

## Interactive menu

`arch-update` opens an `fzf` menu (`gum`, or a numbered list, if `fzf` is
missing). Typing a name searches the packages in your update queue, the
kernel profiles and, from two characters on, the repositories in your
`pacman.conf` (with `pacman -Ss`). It does not search the AUR: AUR packages
appear only if they are in your update queue. Selecting a package shows a
card; it does not install anything.

Repos, AUR, Full (Engine is the same as Full) and Kernel each ask once in
Deck, and Enter means no. `pacman` and `yay` then show their own prompts,
where Enter means yes (`run --yes` passes `--noconfirm` instead). A second
Deck question appears only for the low-space cache cleanup described above. The menu also shows the status, the queue, the plan,
the last run, `pacman.log`, orphans, the Arch news feed (it fetches
`https://archlinux.org/feeds/news/`), CVEs, the schedule, `.pacnew` diffs,
failed units, the catalogue, the share report and the session scanner. The
Schedule entry offers to enable the daily patrol.

AUR updates from the menu and from `arch-update run` use `yay` only.

## Sequence of the unattended update

1. **Check.** Nine checks run concurrently. They change nothing, except that
   the commands you configure as conditions and smoke tests run as written.

   | Check | Fails or blocks when | Exit |
   |---|---|---|
   | root access | running as root, or `sudo -n true` fails | 70 |
   | conditions | no configuration file (70); a configured command exits non-zero (its `exit`, default 20) | 70, 20, 40, 50 |
   | record directory | `keep` is set and the nearest existing folder on its path is not writable or is on the same device as `/` | 50 |
   | pacman lock | `/var/lib/pacman/db.lck` exists | 30 |
   | disk space | less than 6 GiB free on `/` | 30 |
   | name resolution | `archlinux.org` does not resolve | 30 |
   | package database | `pacman -Dk` exits non-zero | 20 |
   | kernel and services | kernel image or initramfs missing; GRUB config without the default kernel; NVIDIA DKMS module missing for an installed kernel (only if `nvidia-dkms` or `nvidia-open-dkms` is installed); a unit that failed after the first check of this run; a Hyprland binary with missing libraries | 30 |
   | programs | a configured `smoke` command exits non-zero | 30 |

   Units that were already failed at the first check of the run are not blamed
   on the update. A check that raises an error counts as "unknown" and blocks
   the run with exit 20. The exit column applies when nothing else decides the
   exit first; see [Exit status](#exit-status).
2. **Update.** Each installed store is updated once. A failing store does not
   stop the others. Before the first update the record is written (see
   `keep`).
3. **Repair.** If a store failed, its failure is looked up in the catalogue of
   known failures. If a repair is recorded for it, the repair runs and the
   sequence returns to step 1. After the checks pass, a store that failed
   with a repairable failure is run again; a store whose failure has no
   repair is not run again. A failed check is looked up the same way. At most
   three repairs run per update.
4. The run ends when the checks pass and no store is left to update. It also
   ends, with the failure reported, when a failure has no recorded repair,
   when the repair could not be attempted, or when the three repairs are
   spent. It ends as well when a check is blocked (database check,
   conditions, root access, record directory) or a check itself failed to
   run. If that happens at the first check, nothing has been updated.

After a repair the checks always run again before any store is updated. A
repair reports only whether it was attempted; its exit status is ignored.
A repair that cannot be attempted ends the run: `quarantine-aur-cache` when
nothing was moved, `rebuild-against-new-libraries` when `unattended_aur` is
off or `yay` is missing.

If the process receives SIGTERM, the command in progress finishes and no
further store is started.

The sequence is the program in `arch_updater/control.tis`, executed by
`arch_updater/tis.py`. A node is limited to 15 instructions and literals to
-999..999.

## Catalogue of known failures

`arch_updater/data/failure-modes.json` is the shipped catalogue (installed under
`~/.local/share/arch-update-deck/arch_updater/data/`). An entry has an `id`,
a `match` (a regular expression searched in the failing command's output),
a `meaning`, a `fix`, and optionally a `repair`. The `fix` is one command to
run by hand. An entry with `"classify_output": false` is never matched against
command output; the checks raise it themselves. The `repair` is one of these
names (`arch_updater/repairs.py`); a name not in this list is ignored, and the
failure is not repaired:

| `repair` | What it does |
|---|---|
| `wait-for-lock` | Waits up to 2 minutes (24 checks, 5 s apart) for the pacman lock to disappear. Never removes it. |
| `trim-cache` | `sudo paccache -rk2`. |
| `wait-for-dns` | `resolvectl flush-caches`, then waits up to 60 s for `archlinux.org` to resolve. |
| `wait-for-mirror` | Waits 60 s. |
| `quarantine-aur-cache` | Renames a `yay` cache directory that has no `PKGBUILD` to `<name>.quarantine-<run id>` in the same directory. Only a real directory directly inside the cache, named in yay's "error downloading sources" line. Nothing is deleted. |
| `rebuild-initramfs` | `sudo mkinitcpio -P`. |
| `rebuild-dkms` | `sudo dkms autoinstall` (for the running kernel; a kernel installed later is built by its pacman hook). |
| `rebuild-against-new-libraries` | `yay -S --rebuild` of the `rebuild` packages named in `smoke`. Only with `unattended_aur` on and `yay` installed. |

The unattended run acts on `repair` only. The `automatic` and `response`
fields in the shipped file are shown by the menu's catalogue view and are not
read by `auto`. In the shipped file `automatic` is true exactly for the entries
that have a `repair`.

When a store fails and no entry matches its output, the failure is added to
`~/.local/state/arch-updater/census.json` as a new entry `LOCAL-<hash>`. Its
`match` is the line that identified the failure (the last line containing
"error", else the last line) with numbers loosened. Its `fix` is
`less +G <run.log>`, its `meaning` says when and where it was first seen, and
`seen` counts how often it came back. It is recognised on later runs. It has
no `repair`, so the store is not retried. To have the failure handled, add a
`fix` or a `repair` to the entry in `census.json`; do not edit the installed
catalogue, which `install.sh` overwrites.

Only failures of the stores (pacman, AUR, Flatpak, Snap) are recorded this
way. A failed check is never added to the census.

## Schedule and ask mode

```bash
arch-update schedule --install-auto
```

Writes `arch-update-auto.service` and `arch-update-auto.timer` to
`~/.config/systemd/user/` and enables the timer. It runs Saturday 12:00 local
time, with up to 30 minutes of random delay, and the timer has
`Persistent=true`: after a missed Saturday (the machine was off, or you were
logged out) the run starts when your user manager next starts, at login or, with
lingering, at boot. The service has `SuccessExitStatus=10 80`, so any other
exit status leaves the unit failed and visible in `systemctl --user --failed`.
Without `"ask"` the unit's status is the status of `arch-update auto`.

With `"ask": true` the timer opens a terminal on `arch-update auto --ask`. It
shows what is waiting and the result of a dry check, and waits for a key:

| Key | Action |
|---|---|
| `y` | Run the update in that window, then wait for Enter. |
| `t` | Show or hide the two nodes of `control.tis` with the instruction each is on. |
| `l` | Show the full package list (when there is a list). Any key returns. |
| anything else, including Enter | Not now. Nothing is updated. |

The wait for an answer ends after `ask_timeout_minutes` (default 30; a number
greater than 0, anything else falls back to 30), and nothing is updated.

Exit status of the scheduled run (`day.scheduled`, `day.ask`):

| What happened | Status |
|---|---|
| The update ran (after `y`, or because no terminal was found and it ran unasked) | Its own status, as in [Exit status](#exit-status). |
| A terminal was found, but neither `DISPLAY` nor `WAYLAND_DISPLAY` is set | 20 (BLOCKED), plus a notification and a line in `ask-window.log`. |
| The answer was "not now", or no answer came in time | 80 (SKIPPED). The reason is in `ask-result.json`. |
| The window closed and left no answer on record | 70 (TOOL_FAILURE), plus a notification and a line in `ask-window.log`. |

If you close the ask window while the update is running, no `ask-result.json`
is left and the run exits 70. The `pacman` session itself continues: it runs
in its own session (`start_new_session` in `shell.py`) and is not signalled by
the window closing.

The terminal emulator's own exit status is never used. 80 is in the unit's
`SuccessExitStatus`, so a declined or timed-out week does not fail the unit.

`ask-result.json` holds `at`, `outcome` (`updated`, `declined` or `timed-out`),
`exit_code` and `reason`. The timer's run deletes it before opening the window
and reads it afterwards.

When you run `arch-update auto --ask` yourself, the command exits with the
update's own status after `y`, with 0 after "not now", and with 80 after a
timeout. (The file records 80 for "not now" in both cases.)

Requirements of ask mode:

- A terminal must be found: `$TERMINAL` if it is set and in `PATH`, otherwise
  the first of `xdg-terminal-exec`, `kitty`, `foot`, `wezterm`, `alacritty`,
  `konsole`, `gnome-terminal`, `xterm`. A `TERMINAL` that is not in that list
  is started with `-e`. If no terminal is found, the update runs without
  asking.
- The terminal is started from the systemd user manager, so that manager's
  environment must contain `DISPLAY` or `WAYLAND_DISPLAY`. The service sets
  only `PATH`; most desktop sessions import the display variables, otherwise
  run `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY`.
  `TERMINAL` is likewise only seen if it is in that environment. If neither
  display variable is set, the weekly update is not run; a notification and a
  line in `ask-window.log` say so.
- The window's output, and these failure messages, go to `ask-window.log` in
  the state directory.

## Results

If `notify-send` exists, every run that is not a dry run sends a desktop
notification: the state and the problems, each with its `fix`. A skipped run
(status 80) sends one too. Urgency is critical unless the status is 0, 10 or
80.

Clicking the notification opens a terminal on the `fix` of the first problem
that has one, or on `arch-update run --mode aur` if only the AUR was held.
This needs a terminal found as described above, `systemd-run`, and a
`notify-send` that supports `--wait` and `--action`. The `fix` runs as your
user; those that start with `sudo` ask for your password in that terminal.

```bash
arch-update auto --status
```

prints the problems left by the last run, each with its `fix`, plus held
stores, `.pacnew` files and a pending reboot. The exit statuses are listed
under [Exit status](#exit-status).

## Daily patrol

```bash
arch-update schedule --install
```

Enables a daily read-only job at 09:30, with up to 15 minutes of random delay
and `Persistent=true`. It writes `arch-update-patrol.service` and
`arch-update-patrol.timer` to `~/.config/systemd/user/`. The Schedule entry in
the menu does the same after a confirmation.

The patrol runs read-only queries: `pacman -Q` for the kernel packages,
`systemctl --failed`, `pacdiff -o`, `dkms status`, `checkupdates`, `yay -Qua`
and `arch-audit`, each only if the program is installed (a few more apply when
Hyprland or an NVIDIA driver is installed). It never installs anything and
never uses `sudo`. `checkupdates` downloads the package databases into a
temporary copy (`${TMPDIR:-/tmp}/checkup-db-<uid>`), not into
`/var/lib/pacman`; `yay -Qua` and `arch-audit` use the network.

It writes `last-patrol.json` and appends to `patrol-history.json`, which keeps
the last 12 patrols. `arch-update share-report` reads `last-patrol.json`.

To stop it: `arch-update schedule --remove`.

## Session scanner

`arch-update scan-sessions` reads local Codex session files
(`~/.codex/sessions/`) and Claude Code session files for the home directory
(`~/.claude/projects/<home path with dashes>/`), and `/var/log/pacman.log`, to
find sessions in which a package-manager command changed the system. It runs
only when you ask for it, from the command line or the menu, and the menu asks
first. It writes `update-sessions.json`, `UPDATE-SESSIONS.md` and
`failure-evidence.json` to `--output-dir` (default `reports/` in the state
directory). In the commands and evidence lines it writes, secrets that look
like `TOKEN=...`, passwords and `Authorization` headers are replaced. Nothing
leaves the machine: the scanner makes no network call.

## Configuration

`~/.config/arch-updater/auto.json` (`$XDG_CONFIG_HOME/arch-updater/auto.json`).
Create a starter file with `arch-update auto --init`; an existing file is never
overwritten. Without this file `auto` exits 70.

```json
{
  "conditions": [
    {"name": "home", "argv": ["am-i-at-home"], "exit": 40},
    {"name": "backup", "argv": ["my-backup-is-fresh"], "exit": 50}
  ],
  "keep": "/mnt/other-disk/arch-update",
  "smoke": [{"argv": ["my-app", "--version"], "rebuild": "my-app-git"}],
  "unattended_aur": false,
  "aur_min_age_days": 4,
  "ask": false,
  "ask_timeout_minutes": 30
}
```

| Key | Meaning |
|---|---|
| `conditions` | Commands that must exit 0 before anything is changed. Each has a 60 s limit. `name` labels it, `argv` is the command, `exit` is the status reported when it fails: 20, 40 or 50. Any other value, or none, means 20. Default: none. |
| `keep` | Directory for the record written before the first update of a run: `packages.txt`, `explicit.txt`, `foreign.txt` and `etc.tar.zst` (the part of `/etc` your user can read; files only root can read are left out, so no password hashes). The last four records are kept. The directory is created (mode 0700, with missing parents) when the record is written. The nearest folder that already exists must be writable and, unless `keep_on_system_disk` is `true`, on a different device than `/`. Default: `keep/` in the state directory. |
| `keep_on_system_disk` | `true` allows `keep` on the same device as `/`. Default `false`. |
| `smoke` | List of `{"argv": [...], "rebuild": "<package>"}`. Each command runs with a 20 s limit; a program that is not installed is skipped, one that exits non-zero is rebuilt from the AUR. The rebuild needs `unattended_aur` and `yay`; without them the failure is reported. |
| `unattended_aur` | `true` updates the AUR without asking. Default `false`. Only the JSON value `true` counts. See [AUR](../README.md#aur). |
| `aur_min_age_days` | With `unattended_aur`: leave out packages whose AUR entry changed within this many days. Default 4. |
| `ask` | `true` opens a terminal and asks before the weekly update. Default `false`. See [Schedule and ask mode](#schedule-and-ask-mode). |
| `ask_timeout_minutes` | How long the ask window waits for an answer. Default 30. |

The check that `keep` is "another disk" compares the device number of `keep`
with that of `/`. Two subvolumes of one btrfs filesystem have different device
numbers, so a second subvolume on the same physical disk passes the check.
Choose a directory on a separate physical disk yourself.

## Exit status

`arch-update auto` exits with these statuses (`arch_updater/exits.py`,
`exit_code` in `arch_updater/auto.py`). A dry run uses the same numbers.

| Status | Name | Meaning | A script should |
|---|---|---|---|
| 0 | IDLE | The last check passed and nothing is left to update. Updates may have been installed. | Nothing. |
| 10 | READY_FOR_REBOOT | As 0, and the module directory of the running kernel is gone, which means a kernel was replaced. | Schedule a reboot yourself; the tool never does. |
| 20 | BLOCKED | The run stopped on a "blocked" or "unknown" result with no more specific status: a condition with `exit` 20 or without a valid `exit`, a failed `pacman -Dk`, or a check that crashed. | Read `auto --status`, fix the cause, run again. |
| 30 | RECOVERY_PENDING | A store failed and is still failed, or a check failed (lock, low space, name resolution, kernel and services, programs) and no repair is recorded, none could be attempted, or the three repairs are spent. A failed store decides the status even if a later check was blocked with 40 or 50, unless the failure is in the record directory (50). SIGTERM before a store has started also gives 30. | Read `auto --status`, run its `fix`, then run again. Do not repeat the run blindly. |
| 40 | NETWORK_BLOCKED | A condition configured with `"exit": 40` failed. | Try again later. |
| 50 | BACKUP_BLOCKED | A condition configured with `"exit": 50` failed, or the record directory is unusable, or the record could not be written. | If it comes from a condition, try again later. Otherwise fix the directory. |
| 70 | TOOL_FAILURE | No configuration file, no passwordless root (or running as root), the tool itself crashed, or its report could not be written. | Fix the setup. Retrying does not help. |
| 80 | SKIPPED | Another non-dry run holds `auto.lock`, or the ask window got "not now" or timed out. | The update did NOT happen this week. If another run holds the lock, that run is the one that counts; if the ask was declined, run it yourself when you want it. |

Other commands exit 0 on success and 1 on an error or a refusal
(2 for a usage error); `arch-update run` also exits 1 if one of its commands
failed.

### A lock that never clears

A pacman lock that does not go away is what a real stale lock looks like: the
run waits three times for two minutes and exits 30. The ask screen shows the
headline `FAILED` and the command to inspect the lock
(`sudo fuser -v /var/lib/pacman/db.lck`); everywhere else, including
`report.json` and `auto --status`, the state is `RECOVERY_PENDING`. The tool
never removes the lock.

## Files

State directory: `~/.local/state/arch-updater/`, or `$ARCH_UPDATER_STATE`.

| Path | Content |
|---|---|
| `~/.config/arch-updater/auto.json` | Configuration. |
| `runs/auto-<time>/run.log` | Output of the commands the unattended run executed through its command wrapper: the store updates, repairs, the `tar` of `/etc`. The probes (`sudo -n true`, `pacman -Dk`, package listings, `checkupdates`) are not logged. |
| `runs/auto-<time>/tokens.jsonl` | Check results, repairs and control-flow events. |
| `runs/auto-<time>/report.json` | Result of the run: `run_id`, `started_at`, `finished_at`, `dry_run`, `exit_code`, `state`, `trouble`, `verdicts`, `updated`, `held`, `repairs`, `pacnew`, `reboot_required`, `wrote` (the run directory), and a `cve` block with the `arch-audit` findings (they never change the exit status). |
| `runs/<time>/` | An interactive `run`: `run.log` (everything it ran), `before.json`, `after.json`, `report.json`. |
| `runs/kernel-<time>/run.log` | Kernel selection. |
| `last-auto.json` | `report.json` of the last `auto` run that was not a dry run. |
| `last-run.json` | Report of the last interactive `run`. |
| `last-patrol.json`, `patrol-history.json` | The last patrol, and a summary of the last 12. |
| `census.json` | Store failures first seen on this machine. |
| `events.jsonl` | Stage events (started, completed, failed) of interactive runs. |
| `keep/` | Pre-update records, unless `keep` is set. |
| `auto.lock` | Lock file for `auto`. It is created by every `auto` run, dry runs included; the lock is the `flock` on it, not the file's existence. |
| `ask-window.log` | Output of the ask-mode terminal, and the reasons it could not run. |
| `ask-result.json` | The answer given in the last ask window. |
| `hud-cache.json`, `deck-book.json`, `deck-book.log`, `last-view.txt` | Menu caches: the queue, the rendered cards and how long they took, and the text of a view when no pager was available. |
| `reports/` | Output of `scan-sessions` by default. |
| `~/.config/systemd/user/arch-update-auto.{service,timer}`, `arch-update-patrol.{service,timer}` | The user units. |
| `/etc/sudoers.d/zzz-arch-update-auto` | The sudoers rules, once you install them. |
| `~/.local/bin/arch-update`, `~/.local/share/arch-update-deck/`, `~/.local/share/applications/arch-update.desktop` | The installation. |
| `/etc/default/grub.arch-updater-<time>.bak` | Backup made by each kernel selection. |
| `~/.cache/yay/<name>.quarantine-<run id>` | Cache directories set aside by `quarantine-aur-cache`. |

Nothing removes these on its own: `runs/` (every `auto` run, dry runs and the
dry check of each ask screen included), `events.jsonl`, `ask-window.log`,
`deck-book.log`, the `grub.arch-updater-*.bak` files and the
`*.quarantine-*` directories grow until you delete them. Only `keep/` is
pruned, to the last four records. Delete a quarantine directory or a GRUB
backup only after checking that you do not need it.

## Environment variables

| Variable | Effect |
|---|---|
| `ARCH_UPDATER_STATE` | State directory. Default `~/.local/state/arch-updater`. |
| `ARCH_UPDATER_HOME` | Directory that contains the `arch_updater` package; read by the launcher. Default `../share/arch-update-deck` next to it. |
| `ARCH_UPDATER_PREFIX` | Installation prefix; read by `install.sh` only. Default `~/.local`. |
| `XDG_CONFIG_HOME` | Where `arch-updater/auto.json` is looked for. Default `~/.config`. |
| `XDG_CACHE_HOME` | Where the `yay` cache is looked for: by the AUR sudoers rules and by `quarantine-aur-cache`. Default `~/.cache`. |
| `TERMINAL` | Terminal for ask mode and click-to-fix. See [Schedule and ask mode](#schedule-and-ask-mode). |
| `NO_COLOR` | Turns colour off on the ask screen. Nothing else reads it. |
| `DISPLAY`, `WAYLAND_DISPLAY` | Read by the scheduled ask run: if neither is set, it does not open a window (status 20). |
| `USER` | Shown in the prompt line of some menu previews. |
| `PYTHONPATH` | The launcher puts the installation directory in front of it. |

The menu clears `FZF_DEFAULT_OPTS` for the `fzf` it starts. The colour and
animation variables of earlier versions (`ARCH_UPDATE_COLOR`,
`ARCH_UPDATE_MOTION`, `COLORTERM`) are no longer read by anything.

## Dry run output

`arch-update auto --dry-run` prints the JSON report on stdout. On stderr it
adds one line saying where the report was written (`wrote` in the report; a dry
run keeps its `report.json` and `tokens.jsonl` there, and `auto.lock` in the
state directory), and, if the root check failed with `NO-ROOT`, a line saying
that this is expected until the sudoers rule is installed. A dry run attempts no
repair, so the `repairs` list shows `"attempted": false`.

## Installing with pip

`pyproject.toml` (setuptools) declares the `arch_updater` package with
`control.tis` and `data/*.json` as package data, the console script
`arch-update = arch_updater.main:main`, and an optional dependency group `test`
(`pytest`). That is the route for `pip install .` (in a virtualenv, or with
`pipx`). Only `install.sh` also writes the `.desktop` file and the launcher in
`~/.local/bin`.

## Testing

```bash
python3 -m pytest -q
```

This needs `pytest`. The tests of the unattended run replace every command,
the name lookup, the AUR request and the sleeps with scripted ones
(`tests/test_auto.py`). This repository does not record which stores or
repairs have been run on a real machine. The `snap` store appears in the
scripted tests only. No test runs the `paru` command line.

To watch a repair on a live system, see "Verifying a repair" in
[START-HERE.md](../START-HERE.md).
