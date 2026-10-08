# Arch Update Deck

An updater that learns.

`arch-update` opens one screen. It lists what is waiting, checks that the
machine is in order, shows what last time left undone, and asks one question.
`y` updates pacman, Flatpak and Snap on that same screen (the AUR too, once you
turn it on). Any other key changes nothing.

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

Before the first change it saves your package lists and the readable part of
`/etc`. Then it updates. Then it checks the machine again. Most weeks that is
the whole story.

## When something breaks, it writes it down

A failure that is still standing at the end of a run leaves a **brief**: the
lines of the log that matter, where to look, which known cases the output
matches, and the one command that saves a fix. `arch-update brief` prints the
newest one. Read it yourself, or hand it to whatever assistant you use.

Fix it and save the fix in one step:

```bash
arch-update learn --match '<regex>' --how '<what was wrong>' --command '<sh command>'
```

`learn` is strict. The pattern must find the failure in the brief's log. The
command runs, the failed step runs again, and only if that passes is the case
saved. A fix that does not hold is not a case.

From then on, when that failure comes back, the command runs by itself, with
nobody asked. What you learned is chosen before anything that shipped.

Thirty-two known failures ship with the tool, each with a pattern, a meaning
and a fix a person can run. Yours join them in `census.json`.

## Then pass it on

```bash
arch-update census           # what this machine learned: [ ] only here · [x] handed upstream · * shipped
arch-update census --share   # one GitHub issue, filled in, home folder and login taken out; you press Submit
```

The tool sends nothing by itself. Right after `learn`, and once in the weekly
notice, each new fix is offered for sharing a single time, never again;
`"share": false` in the configuration turns even the offer off.

This is the ask. It has met one machine so far. Show it yours. Download it,
break it, fix it, send your census, and let us build one shared base of what
goes wrong on Arch and what settles it.

## Dotfiles follow upstream, your edits win

List a git repository under `dotfiles` (end-4's dots-hyprland, say) and once a
month it is brought to upstream's newest release tag. Your uncommitted edits are
committed and tagged first. The merge is `git merge -X ours`: where you and
upstream touched the same lines, yours stay; everything else comes in. Every
changed shell, fish, Python, QML, Lua, JSON or TOML file must still parse, or it
is given back to you as it was. The repository's installer runs after every
move.

`arch-update dotfiles --drift` shows what your edits hold back, as git marks
it. `--suggest` lists what else on the machine was cloned from git and could be
followed. `--add PATH` follows one. A merge that fails leaves a brief like any
other failure, and a fix you `learn` for it replays inside the next merge.

## It stays out of your way

The weekly run fires three minutes after you log in, then once a day, and only
does anything when the last visit is seven days old. It waits while a game is
running, while the machine is under CPU, memory or I/O pressure, and while you
are on battery, for three days at most. When it does run, it takes a quarter of
your cores at a reduced quota, so the fans stay down; an update you start by
hand gets no such ceiling, because you are waiting for it. With `home_bssid`
set, nothing changes unless the Wi-Fi is on one of your own access points, known
by hardware address, not by name. With `"ask": true` the timer opens the screen
above and waits for your `y`.

It never reboots, never merges a `.pacnew`, never removes the pacman lock, and
never stores your password. [REFERENCE](docs/REFERENCE.md#safety-statements-in-detail)
spells each of those out.

## Requirements

- Python 3.11 or later (standard library only) and `pacman`.
- To update: `sudo`, `pacman-contrib` (`checkupdates`, which also needs
  `fakeroot`) and `tar` with `zstd`. Without `checkupdates` the queue cannot be
  read and the screen asks no question; without `tar` and `zstd` the record of
  `/etc` cannot be written and the update does not start.
- For the weekly timer: `visudo` and a systemd user session.
- Optional: `yay` or `paru`, `flatpak`, `snap`, `libnotify`, `arch-audit`,
  `resolvectl`, a terminal the tool recognises. The
  [reference](docs/REFERENCE.md#requirements) says what each one adds.

## Install

```bash
git clone https://github.com/zzallirog/arch-update-deck && cd arch-update-deck && ./install.sh
command -v arch-update
```

No root. The sources go to `~/.local/share/arch-update-deck/`, the launcher to
`~/.local/bin/arch-update`, a `.desktop` file to `~/.local/share/applications/`.
`ARCH_UPDATER_PREFIX` moves all three. Run the installer again after every
update of the source tree. If `command -v` prints nothing, add `~/.local/bin`
to your `PATH`.

Then `arch-update`, read the screen, press `y`. The first run writes a starter
configuration and says so under `PENDING`. That is the whole first update.
[START-HERE.md](START-HERE.md) walks through making it weekly and unattended.

## Where things live

| | |
|---|---|
| Configuration | `~/.config/arch-updater/auto.json` |
| State: runs, briefs, census, the pre-update record | `~/.local/state/arch-updater/` |
| The sudoers rules, once you install them | `/etc/sudoers.d/zzz-arch-update-auto` |

Version 0.20.01. Written for one machine first; expect rough edges, and send
them in. [docs/REFERENCE.md](docs/REFERENCE.md) has every command, key, exit
status, configuration key and file.

## Removing it

```bash
arch-update schedule --remove
arch-update schedule --remove-auto
sudo rm -f /etc/sudoers.d/zzz-arch-update-auto
rm -rf ~/.local/share/arch-update-deck ~/.local/bin/arch-update ~/.local/share/applications/arch-update.desktop
```

The configuration and state directories stay until you delete them.

## License

MIT. See [LICENSE](LICENSE).
