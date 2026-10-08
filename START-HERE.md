# Quick start

Sets up the weekly unattended update and checks that it works. See
[README.md](README.md) for what the tool does and does not do, and
[docs/REFERENCE.md](docs/REFERENCE.md) for details. The sudoers rule (step 4)
comes before the first dry run on purpose: without it the dry run stops at the
root check.

## Before you start

You need `sudo` (with `visudo`), `tar` with `zstd`, `pacman-contrib`
(`checkupdates`, `paccache`) and a systemd user session. Run
`command -v sudo visudo tar zstd checkupdates fakeroot paccache systemctl`: a
name missing from the output is not installed (`sudo`, `pacman-contrib` and
`fakeroot` are not in every base install; `checkupdates` needs `fakeroot`). Everything else is optional; the reference says what is lost
without each program.

## 1. Install

```bash
./install.sh
command -v arch-update
```

If the second command prints nothing, `~/.local/bin` is not in `PATH` (the
installer prints a warning). Add it, then open a new shell. In bash:
`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc`. In fish:
`fish_add_path ~/.local/bin`.

## 2. Create the configuration

```bash
arch-update auto --init
```

This writes `~/.config/arch-updater/auto.json` (an existing file is never
overwritten) with `{"conditions": [], "unattended_aur": false}`. Without this
file the unattended update exits 70.

## 3. Set the record directory

Before the first update of each run, the package lists and the part of `/etc`
your user can read are saved. Add `keep`, a directory on another physical disk:

```json
{"conditions": [], "keep": "/mnt/second-disk/arch-update", "unattended_aur": false}
```

The directory is created when the record is written, with any missing parents.
The nearest folder that already exists must be writable and on a different
device than `/`; if not, the update does not start. That check compares device
numbers, so on btrfs a second subvolume of the same disk counts as "another
disk": the tool cannot tell, check yourself. Without `keep`, the record goes to
`~/.local/state/arch-updater/keep/`, on the system disk.

## 4. Allow the update commands without a password

Print the rules and read them first:

```bash
arch-update auto --sudoers
```

Each rule is `<user> ALL=(root) NOPASSWD: <program> <fixed arguments>`, for
`true`, `pacman -Syu --noconfirm`, `paccache -rk2`, `mkinitcpio -P`,
`dkms autoinstall` (and `snap refresh`). A program that is not found in
`/usr/bin`, `/usr/sbin`, `/bin` or `/sbin`, or whose file root does not own or
others can write, gets a `# skipped <program>` line instead of a rule; look for
such lines. A `# skipped snap` line is normal if you do not use snap. Then
install the rules. This works in bash and fish and stops if `visudo` finds an
error:

```bash
sh -c 'f=$(mktemp) && arch-update auto --sudoers > "$f" && visudo -cf "$f" && sudo install -m 0440 "$f" /etc/sudoers.d/zzz-arch-update-auto; rm -f "$f"'
```

Keep the file name: sudo applies the last matching rule, and this file must sort
after the rule that grants all commands with a password. Check with
`sudo -n true && echo passwordless-root-works`; an error such as "a password is
required" means the rule is not in effect (`sudo -l` shows which rule wins).

## 5. Look at the machine

```bash
arch-update auto --dry-run
```

This prints a JSON report. It installs nothing and repairs nothing. It does
create `~/.local/state/arch-updater/runs/auto-<time>/` (`report.json`,
`tokens.jsonl`) and the file `auto.lock`; a line on stderr says where. It runs
your configured conditions, and `arch-audit` if installed, and can take up to a
minute. Look at:

- `"state": "DRY_RUN"` and `"exit_code"`. The command's exit status is the same
  number. `0` or `10` (a reboot is pending) is good.
- `"verdicts"`: every `"status"` should be `"pass"`.
- `"trouble"`: should be `[]`. Anything in it comes with a `fix`.
- `"cve"`: `"known": false` means `arch-audit` is not installed or the scan
  failed; the `error` field says which. It never affects the exit status.

Exit status 70 with `"case": "NO-ROOT"` is expected until step 4 is done. The
reference lists the other statuses.

## 6. Enable the timer

```bash
arch-update schedule --install-auto
loginctl enable-linger "$USER"
systemctl --user list-timers arch-update-auto.timer
```

The first command installs `arch-update-auto.timer` as a systemd user unit:
Saturday 12:00, up to 30 minutes of random delay, `Persistent=true`, so a missed
run starts when your user manager next starts. The second lets that manager run
while you are logged out (otherwise the weekly run happens only while you are
logged in). The third shows the next run. A finished run sends a desktop
notification, if `notify-send` is installed.

To be asked before each update, add `"ask": true` to the configuration: the
timer then opens a terminal on `arch-update auto --ask`, where `y` updates and
anything else (Enter included) does nothing. It needs a terminal the tool
recognises and `DISPLAY` or `WAYLAND_DISPLAY` in the systemd user manager's
environment; the reference has the details, the time-out and the exit statuses.
To see the prompt now, run `arch-update auto --ask` in a terminal.

Optional: `arch-update schedule --install` adds the daily read-only patrol;
`schedule --remove-auto` and `--remove` delete the two timers again.

## Verifying a repair

This shows the tool recognising a known failure, the pacman lock, and waiting
for it to clear. The commands below create a lock file themselves; a root timer
removes it after 180 seconds, only if it still contains the text
`arch-update-demo`, so a lock made by a real pacman is never touched. The timer
lives in the system manager, so closing the terminal or pressing Ctrl-C does not
leave the lock behind. The commands work in bash and fish:

```bash
ls /var/lib/pacman/db.lck
sudo systemd-run --quiet --on-active=180 /usr/bin/sh -c '/usr/bin/grep -qx arch-update-demo /var/lib/pacman/db.lck && /usr/bin/rm -f -- /var/lib/pacman/db.lck'
echo arch-update-demo | sudo tee /var/lib/pacman/db.lck
```

The `ls` must say `No such file or directory`. If it prints the path, a lock
exists already: stop and leave it alone.

**A. Only look.** Within the 180 seconds, run `arch-update auto --dry-run`.
Expected: exit status 30 and `"state": "DRY_RUN"`; in `"trouble"` an entry with
`"probe": "lock"`, `"case": "PACMAN-LOCK"` and
`"fix": "sudo fuser -v /var/lib/pacman/db.lck"`; in `"repairs"`
`{"case": "PACMAN-LOCK", "repair": "wait-for-lock", "attempted": false}`. The
repair is listed but not run, because a dry run attempts no repair.

**B. Watch the repair.** This is a real update: after the repair the tool
updates your system. Wait until `ls` says `No such file`, create the lock again
with the three commands above, and run `arch-update auto --ask`. The screen
reads the queue and does a dry check first (a few seconds, longer if
`checkupdates` is slow); the `MACHINE` line shows
`pacman is busy: /var/lib/pacman/db.lck`. Press `y` before the 180 seconds are
over; if the lock is already gone, the run just updates and shows no repair.
Expected lines after `y` (one `updating` line per installed store):

```
checking   pacman is busy: /var/lib/pacman/db.lck
failure    seen before, repairing the same way  PACMAN-LOCK
checking   machine is fine
record     <keep directory>/auto-<time>
updating   repositories …  <n> changed
checking   machine is fine
```

The repair waits up to two minutes for the lock, so the second line appears once
the lock is gone; if the two minutes pass first, it appears again and the tool
waits again, up to three times. Then the screen ends with `DONE`. (A lock that
never clears ends with exit 30 and the headline `FAILED`; the reference
explains.)

If four minutes after creating the demo lock `ls /var/lib/pacman/db.lck` still
prints the path (for example, the machine rebooted and the timer was lost), run
`cat /var/lib/pacman/db.lck`. Only if it prints `arch-update-demo` is it the demo
lock, and you can remove it with `sudo rm /var/lib/pacman/db.lck`. Otherwise
leave it.

## Troubleshooting

`arch-update auto --status` lists the problems left by the last run, each with a
`fix`: the command to run. Clicking the desktop notification opens a terminal on
that command; a `fix` that starts with `sudo` asks for your password there.

`runs/auto-<time>/run.log` in `~/.local/state/arch-updater/` holds the update,
repair and `tar` commands of a run (not the read-only checks; their results are
in `report.json`). A store failure seen for the first time is recorded in
`census.json` there, with `less +G <run.log>` as its `fix`; to have it handled
next time, put the command that resolves it into that `fix` field.

## AUR

The timer does not update the AUR: use `arch-update run --mode aur` (needs `yay`)
or click the notification. Before you turn on `unattended_aur`, read the README
section "AUR": it builds packages without showing you the `PKGBUILD`, and its
sudoers rules let any process running as you install an arbitrary package as root.
