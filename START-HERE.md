# Start here

Install, run `arch-update`, read the screen, press `y`: that is the whole first
update. "Make it weekly and unattended" is optional. See [README.md](README.md)
and [docs/REFERENCE.md](docs/REFERENCE.md). You need `sudo`, `tar` with `zstd` and
`pacman-contrib` (`checkupdates`, which needs `fakeroot`).

## 1. Install

```bash
./install.sh && command -v arch-update
```

If it prints nothing, add `~/.local/bin` to `PATH` (README, Install).

## 2. Update

```bash
arch-update
```

The first time, the tool writes a starter configuration to
`~/.config/arch-updater/auto.json` and says so under `PENDING`. The screen shows
the packages waiting with their source (`REPO`, `AUR · BY HAND`, `FLATPAK`,
`SNAP`), `TOTAL`, `MACHINE` (`fine · 8 checks`, or the failed checks in red),
`PENDING` (what the last update left undone, with the command that fixes it) and
the question `UPDATE NOW?`. A dim line offers `l` (end of the last log) and `k`
(kernel profiles). The question is left out when the repository list cannot be
read, and when only AUR packages wait with the AUR off; the screen then says
what to do by hand (`yay -Sua`).

Press `y`. If `sudo` needs a password and you are at a terminal, the tool runs
`sudo -v` and you type the password there; it goes to `sudo`, not to this tool.
A refused password ends the visit with exit status 70 and nothing changed. The
update then runs on the same screen, one line per step (sudo's ticket is renewed
every minute meanwhile), and ends with one block:

```
 updated: repositories 1
   kwin   6.7.5-1.1 → 6.7.5-3.1

 DONE · REBOOT NEEDED   [Enter] close
```

A failure ends in red `FAILED` and the command that fixes it. `REBOOT NEEDED`
means a kernel was replaced; reboot yourself. Enter closes the screen. At the
question, Enter, `n`, `q`, Esc or any single key except `y`, `l` and `k` means
not now: nothing is changed (arrow keys are ignored). Ctrl-C is safe: a running
package command ends by itself, nothing new starts, the exit status is 130. AUR
rows are listed but not built unless you turn the AUR on. Before the first
change the tool saves the package lists and the readable part of `/etc` to
`~/.local/state/arch-updater/keep/`. If you do not want a weekly update, you are
done.

## Make it weekly and unattended

### A. Configuration and record directory

```bash
arch-update auto --init
```

Writes `~/.config/arch-updater/auto.json` (`{"conditions": [],
"unattended_aur": false}`); it never overwrites a file, and the first
`arch-update` already wrote one. Without it `arch-update auto` exits 70. Then add
`keep`, a directory on another physical disk, for the record saved before each
update:

```json
{"conditions": [], "keep": "/mnt/second-disk/arch-update", "unattended_aur": false}
```

The nearest folder that already exists must be writable and on a different
device than `/`, or the update does not start. The check compares device
numbers, so on btrfs a second subvolume of the same disk passes: check yourself.

### B. Allow the update commands without a password

The timer has no terminal for a password. Print the rules and read them
(`arch-update auto --sudoers`). Each rule is
`<user> ALL=(root) NOPASSWD: <program> <fixed arguments>`, for `true`, `pacman -Syu --noconfirm`, `paccache -rk2`, `mkinitcpio -P`,
`dkms autoinstall` (and `snap refresh`). A program missing from `/usr/bin`,
`/usr/sbin`, `/bin` and `/sbin`, not owned by root or writable by others gets a
`# skipped <program>` line instead (normal for snap if you have none). Install
the rules; this stops if `visudo` finds an error:

```bash
sh -c 'f=$(mktemp) && arch-update auto --sudoers > "$f" && visudo -cf "$f" && sudo install -m 0440 "$f" /etc/sudoers.d/zzz-arch-update-auto; rm -f "$f"'
```

Keep the file name: sudo applies the last matching rule, so this file must sort
after the rule that grants all commands with a password. Check with
`sudo -n true && echo passwordless-root-works`; if it asks for a password the
rule is not in effect (`sudo -l` shows which one wins).

### C. Look at the machine

```bash
arch-update auto --dry-run
```

Prints a JSON report; installs and repairs nothing. It writes a
`runs/auto-<time>/` directory in the state directory (a line on stderr says
where), runs your conditions and `arch-audit` if installed, and can take up to a
minute. Look at `"exit_code"` (also the exit status; `0` or `10`, a reboot
pending, is good), `"verdicts"` (every `"status"` should be `"pass"`),
`"trouble"` (should be `[]`; each entry has a `fix`) and `"cve"` (`"known":
false` means `arch-audit` is missing or failed; it never changes the exit
status). Exit 70 with `"case": "NO-ROOT"` is expected before step B.

### D. Enable the timer

```bash
arch-update schedule --install-auto
loginctl enable-linger "$USER"
systemctl --user list-timers arch-update-auto.timer
```

Installs a systemd user timer: Saturday 12:00, up to 30 minutes of random
delay, `Persistent=true` (a missed run starts when your user manager next
starts). Lingering keeps that manager running while you are logged out.

To be asked first, add `"ask": true` to the configuration. The timer then opens
a terminal with the same screen: `y` updates, anything else does nothing, no
answer within `ask_timeout_minutes` (default 30) skips the week. You type the
password in that window, so this mode works without step B. It needs a terminal
the tool recognises and a display in the systemd user manager's environment
(reference). `schedule --install` adds the daily read-only patrol;
`--remove-auto` and `--remove` delete the timers.

## Verifying a repair

The tool meets a known failure, the pacman lock, and waits for it. Part 1 needs
step B: without the rule the dry run stops at the root check with exit 70. A
root timer removes the demo lock after 180 seconds, only if it still contains
`arch-update-demo`, so a real pacman lock is never touched. Bash and fish:

```bash
ls /var/lib/pacman/db.lck
sudo systemd-run --quiet --on-active=180 /usr/bin/sh -c '/usr/bin/grep -qx arch-update-demo /var/lib/pacman/db.lck && /usr/bin/rm -f -- /var/lib/pacman/db.lck'
echo arch-update-demo | sudo tee /var/lib/pacman/db.lck
```

The `ls` must say `No such file or directory`; if it prints the path, a lock
exists already: leave it alone.

**1. Only look.** Within the 180 seconds run `arch-update auto --dry-run`.
Expected: exit status 30, `"state": "DRY_RUN"`; in `"trouble"` an entry with
`"probe": "lock"`, `"case": "PACMAN-LOCK"` and
`"fix": "sudo fuser -v /var/lib/pacman/db.lck"`; in `"repairs"`
`{"case": "PACMAN-LOCK", "repair": "wait-for-lock", "attempted": false}` (a dry
run attempts no repair).

**2. Watch the repair.** This is a real update. Wait until `ls` says `No such
file`, create the lock again with the three commands, and run `arch-update`.
`MACHINE` shows `pacman is busy: /var/lib/pacman/db.lck` in red. Press `y`
within the 180 seconds (if the lock is gone, the run just updates). Expected
lines after `y` (one `updating` line per installed store):

```
checking   pacman is busy: /var/lib/pacman/db.lck
failure    seen before, repairing the same way  PACMAN-LOCK
checking   machine is fine
record     <keep directory>/auto-<time>
updating   repositories  <n> changed
checking   machine is fine
```

The repair waits up to two minutes, so the second line appears once the lock is
gone; if the two minutes pass first, the first two lines repeat, up to three
times. The screen then ends with `DONE`; a lock that never clears ends with exit
30 and `FAILED`. If the lock is still there after four minutes (the timer was
lost), `cat /var/lib/pacman/db.lck`: only if it prints `arch-update-demo` may you
`sudo rm /var/lib/pacman/db.lck`.

`arch-update auto --status` lists what is left, each with a `fix`. Read
[AUR](README.md#aur) before turning `unattended_aur` on.
