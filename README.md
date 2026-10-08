# Arch Update Deck

`arch-update` opens one screen. It lists what is waiting, checks that the
machine is in order, shows what still hangs from last time, and asks one
question. Answer `y` and it updates pacman, Flatpak and Snap on that same
screen (the AUR too, if you turn that on). Any other answer changes nothing.

```text
 UPDATE DAY                                                                         Thu 08.10 04:09

 PACKAGE           INSTALLED    AVAILABLE    FROM
 zen-browser-bin   1.23b-1      1.23.1b-1    REPO
 discord-canary    1.0.2139-1   1.0.2140-1   AUR · BY HAND

 TOTAL    1 from repositories · 1 from AUR (by hand: yay -Sua)

 MACHINE  fine · 8 checks
 PENDING  nothing since last time

 UPDATE NOW?  [y] yes   [N] not now   Enter means no

 l last run · k kernel
```

The same update can also run by timer, with or without asking you first. Before
it changes anything the tool checks the machine and saves a record of the
package lists and `/etc`; afterwards it checks again. A failure that has
happened before is repaired in a fixed way. Any other failure is reported with
a command to run by hand.

It is for someone who administers their own Arch machine. It does not read the
Arch news, and it does not replace knowing how to repair a system that no
longer boots.

## Requirements

- Python 3.11 or later (standard library only) and `pacman`.
- To update: `sudo` (it asks for your password once, in the terminal),
  `pacman-contrib` (`checkupdates`, which also needs `fakeroot`; without it the
  repository list cannot be read) and `tar` with `zstd` (for the record of
  `/etc`; without it the update does not start).
- For the weekly timer: `visudo`, a systemd user session and, to run while you
  are logged out, lingering (`loginctl enable-linger`).
- Optional: `yay` or `paru` (AUR: the first one installed lists the waiting
  packages and, with the AUR switched on, builds them; only `yay` has sudoers
  rules), `flatpak`, `snap`, `libnotify`, `arch-audit`, a terminal the tool
  recognises, `resolvectl`. The reference lists what is lost without each.

## Status

Version 0.20.0. Written for one machine first; tested on Arch with systemd;
expect rough edges. Issues welcome.

## Install

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

If `command -v arch-update` prints nothing, `~/.local/bin` is not in `PATH`:

```bash
# bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
# fish
fish_add_path ~/.local/bin
```

## First run

1. `arch-update`
2. Read the screen: the package table, `MACHINE`, `PENDING`.
3. Press `y` to update. Anything else (Enter, `n`, `q`, Esc) does nothing. There
   is no question when the repository list cannot be read, or when only AUR
   packages wait and the AUR is off; the screen says what to do by hand.
4. Type your sudo password when asked. It goes to `sudo`, not to this tool.
5. Wait. The screen shows each step and ends with what changed, what failed
   (with the command that fixes it) and `REBOOT NEEDED` if a kernel was replaced.
   Ctrl-C is safe: the running package command ends by itself, nothing new
   starts, and `arch-update` finishes with exit status 130.
6. Reboot yourself if it says so. The tool never does.

No sudoers rule and no configuration are needed. The first `arch-update` writes
a starter configuration file and says so under `PENDING`.
[START-HERE.md](START-HERE.md) has the optional weekly unattended update.

## What it does and does not do

- **It never reboots.** No code path runs `reboot`, `shutdown` or `poweroff`. A
  pending reboot is shown as `REBOOT NEEDED` (exit status 10 for `auto`), and
  `auto --status` prints `systemctl reboot` as text for you to run.
- **It never merges a `.pacnew` file.** They are found with `pacdiff -o`
  (`engine._pacnew_files`), which only lists them. The result screen prints
  `compare: sudo pacdiff` next to each one. No code writes to them.
- **It never removes the pacman lock** `/var/lib/pacman/db.lck`. The only
  operation on it is `Path.exists()` (`watch.probe_lock`,
  `repairs.wait_for_lock`, `engine.snapshot`). The update waits for it up to
  three times for two minutes and then stops with exit 30.
- **The sudoers rules are exact commands.** `auto --sudoers` prints one rule per
  command with its arguments fixed (`auto.sudoers_rules`). Each program is
  looked up only in `/usr/bin`, `/usr/sbin`, `/bin` and `/sbin`, never in your
  `PATH`, and only a file owned by root that group and others cannot write gets
  a rule (`Shell.root_binary`); otherwise the output has a `# skipped` line.
  `--aur` adds one more exact line, for the root shim: see [AUR](#aur).
- **It never stores your password.** At a terminal it runs `sudo -v` and you
  type the password to `sudo`. While the update runs it refreshes sudo's ticket
  once a minute (`sudo -n -v`, `keepalive.py`) and stops when the update ends.
- Package and boot-file transactions (`pacman`, `yay`, `paru`, `paccache`,
  `mkinitcpio`, `dkms`, `snap`, `flatpak`, `grub-mkconfig`) never get a time
  limit (`shell.TRANSACTION_TOOLS`).
- The update runs `pacman -Syu --noconfirm` without the Arch news. A conflict
  that pacman cannot settle that way makes the store fail and is reported.
- It takes no shutdown or sleep inhibitor. A logout, suspend or power-off during
  an update is not guarded. Ctrl-C never kills the package manager; it waits for
  it to end.
- It has no command to restore the record it writes before an update.
- Free space on `/`: below 6 GiB the repair runs `sudo paccache -rk2`, which
  deletes cached package files except the two newest versions of each package.
  If space is still low after three repairs, the update stops.
- `arch-update kernel` installs a kernel package and, with GRUB, rewrites
  `GRUB_DEFAULT`. It needs a confirmation or `--yes`. It backs up
  `/etc/default/grub` first and restores the backup if writing or regenerating
  `grub.cfg` fails; if the restore itself fails it says so, with the backup's
  path. The backups are never cleaned up.
- Some files grow without limit: `runs/` (one directory per update and per dry
  run; opening the screen writes none), logs, grub backups, quarantined AUR
  caches. The reference lists them.

## AUR

Update Day lists AUR packages and marks them `AUR · BY HAND`. With
`"unattended_aur": false` (the default) `y` does not build them; the result
screen says so, and you update them yourself with `yay -Sua`.

With `"unattended_aur": true` and `yay`:

- `yay -Sua --noconfirm --answerclean None --answerdiff None --answeredit None`
  builds the packages. Nobody reads the `PKGBUILD` or the diff before it runs,
  and a build script runs as your user.
- Before building, the names of all your foreign packages (`pacman -Qqm`) are
  sent to `https://aur.archlinux.org/rpc/v5/info`. Packages whose AUR entry
  changed within `aur_min_age_days` (default 7) are skipped. So are VCS
  packages (`-git` and the like), orphans, and packages whose maintainer changed
  since you last had them up to date; those wait for you. This is a cool-down,
  not a check of what the package does.
- yay does not get root. Where it would run `sudo pacman -U`, it runs
  `arch-update aur-handoff`, which puts the built files in
  `~/.cache/arch-updater/aur-stage` and runs a root shim with no arguments. The
  shim copies the files into a folder only root can write and installs only an
  update of a foreign package you already have, with no install script, no
  setuid file, no file capability, and every file where nothing running as root
  reads it (`/opt`, `/usr/bin`, desktop files, icons, documentation, its own
  folder under `/usr/lib` or `/usr/share`). Anything else is refused and waits
  for you. A package that ships a system service, a dkms module or files in
  `/etc` is never installed this way.
- Install the shim and its sudoers rule (`arch-update auto --sudoers --aur`)
  as in [START-HERE.md](START-HERE.md). The rule names the shim with `""`: no
  argument, so nothing in it comes from your account.

The shim cannot tell whether the package is what the `PKGBUILD` meant: the
build ran as you. What it closes is the way from your account to root through
an AUR install.

## Other commands

| Command | Action |
|---|---|
| `arch-update status [--json]` | Queue, kernel, disk, failed units, `.pacnew` files. |
| `arch-update attest` | Check kernel, initramfs, DKMS, failed units and reboot need. |
| `arch-update kernel [profile] [--dry-run] [--yes]` | List kernel profiles, or install one and make it the GRUB default. |
| `arch-update auto` | The same update without questions; what the timer runs. |
| `arch-update schedule` | Show, install or remove the weekly update and the daily patrol. |
| `arch-update vault`, `cve`, `patrol`, `share-report`, `scan-sessions` | Inspection commands; see the reference. |

## Removing it

There is no uninstall command for the files. Stop and delete both timers,
remove the sudoers file if you made one, then the installed copies:

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

[docs/REFERENCE.md](docs/REFERENCE.md) has everything else: every command and
flag, the keys on the screen, the exit statuses, every configuration key, every
file the tool creates, the checks, the catalogue and the repair names, the
schedule and ask mode, the patrol, the session scanner, the environment
variables, `pip install`, and testing.

## License

MIT. See [LICENSE](LICENSE).
