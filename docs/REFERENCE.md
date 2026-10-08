# Reference

Details for [README.md](../README.md), version 0.18.0. Nothing here is needed
for a first update; see [START-HERE.md](../START-HERE.md) for that.

## Requirements

By what each program is used for.

| Needed for | Program | Without it |
|---|---|---|
| Everything | Python 3.11 or later (standard library only), `pacman` | Does not run. |
| Updating | `sudo` (package `sudo`; not part of a base install) | No root: the update stops with exit 70. |
| Weekly timer without asking | `visudo` (package `sudo`) | The sudoers rule cannot be checked before it is installed. |
| Updating | `tar` with `zstd` | The record of `/etc` cannot be written, so the update does not start (exit 50). |
| Update Day, patrol, `status` | `checkupdates` (package `pacman-contrib`; it also needs `fakeroot`) | The repository queue is shown as unread (`cannot read the repositories: install pacman-contrib`; in `status`, `?`). Update Day then asks no question, because `y` would update blind; `arch-update auto` still runs. |
| Low-space repair | `paccache` (`pacman-contrib`) | No cleanup. The sudoers rule for it is not generated. |
| `.pacnew` listing | `pacdiff` (`pacman-contrib`) | No `.pacnew` files are listed. |
| Timers | `systemd --user`, `systemctl` | `schedule` cannot install the timers. |
| Weekly run while logged out | Lingering: `loginctl enable-linger` | The user manager, and with it the timer, runs only while you are logged in. |
| AUR | `yay` or `paru`: the first one installed lists the waiting packages (Update Day, `status`, patrol) and, with `unattended_aur`, builds them. Rules for `auto --sudoers --aur` exist for `yay` only | No AUR rows, no AUR updates. See [AUR](../README.md#aur). |
| Flatpak, Snap | `flatpak`, `snap` | Those stores are skipped. |
| Desktop notice | `notify-send` (package `libnotify`) | No notification is sent. |
| Click-to-fix | `systemd-run`, a recognised terminal (see [Schedule and ask mode](#schedule-and-ask-mode)), and a `notify-send` that supports `--wait` and `--action` | The notice has no click action. |
| Ask mode | A recognised terminal, and a graphical session known to the systemd user manager | The weekly run updates without asking. |
| CVE report | `arch-audit` (used by `cve`, the patrol and the report of `auto`; not by the look on Update Day) | The report says the CVE state is unknown. |
| DNS repair | `resolvectl` | The repair `wait-for-dns` flushes nothing and only waits. |
| Hyprland health check | `ldd` | Not checked. Only done if `Hyprland` is installed. |
| `kernel`, `k` | `script` (optional) | The output of the kernel commands is piped and logged line by line instead of through `script` (`engine.stream_command`). |

## Update Day

`arch-update` with no command opens one screen. It does not need a
configuration: if `~/.config/arch-updater/auto.json` is missing it writes the
starter one first and says so in a dim line under `PENDING`.

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

The page is drawn at once, and the queue and the checks fill in as they are
read.

| Part | Content |
|---|---|
| Header | `UPDATE DAY`, weekday, date and time. |
| Table | One row per waiting package: `PACKAGE`, `INSTALLED`, `AVAILABLE`, `FROM`. `FROM` is `REPO`, `AUR`, `FLATPAK` or `SNAP`; an AUR row says `AUR · BY HAND` unless `unattended_aur` is `true`. Up to 124 columns wide. On a narrow terminal `INSTALLED` is dropped first, then `AVAILABLE`. If the rows do not fit the height, the table keeps its header, as many rows as fit (at least one) and a line `+ N more`; it never scrolls. |
| `TOTAL` | How many packages come from each source. `(by hand: yay -Sua)` follows when AUR rows are present and `unattended_aur` is off. A source that could not be read is named in red (`cannot read the repositories: install pacman-contrib` or `checkupdates failed`; `cannot read the snap queue`). `nothing waiting` is printed only when every queue was read and all are empty. |
| `MACHINE` | `fine · N checks`, where N is the number of verdicts: 8 with no configured conditions, plus one per condition. A failed check is a red line `<check>: <detail>`; the checks and their screen names are in the [table](#sequence-of-the-update). If the root check is the one that fails, you are not root and stdin and stdout are terminals, it counts as passed and the line ends `sudo asks for your password on yes`. If `sudo` is not installed the line is red and says `sudo is not installed: install it, y cannot continue`, and `y` ends with exit status 70. |
| `PENDING` | The problems left by the last real update (`last-auto.json`), one red line each: `<check> → <fix>`. `nothing since last time` if none. When you are at a terminal as a user with `sudo` installed, entries of the cases `NO-ROOT` and `SUDO-TIMESTAMP-EXPIRED` are left out, because `y` asks for the password; `auto --status` still lists them. Notes (the starter-configuration notice; `weekly update is off: arch-update schedule --install-auto` when the weekly timer is known not to be installed) are dim lines under it. |
| Question | `UPDATE NOW?  [y] yes  [N] not now  Enter means no`, then the dim footer `l last run · k kernel`. |

Package names, versions, build output and logs come from other people. Every
word that reaches the screen goes through `clean()` (`arch_updater/textsafe.py`),
which removes escape sequences (colour, cursor moves, titles, hyperlinks, the
clipboard sequence OSC 52, device-control strings) and every control character
except tab and newline. A table cell is one line, with runs of whitespace collapsed to one space. `status`,
`auto --status` and the kernel list are cleaned the same way.

### The look

The checks that fill `MACHINE` run on the main thread while the queue is read on
a second one. They are `auto.look()`: one pass of the nine checks kept in
memory. It writes no `runs/` directory, no report and no `ask-result.json`, takes
no lock and does not run `arch-audit`. Your configured conditions and `smoke`
commands do run. Only an update that really starts creates a `runs/auto-<time>/`
directory.

### When no question is asked

| Situation | What the page says | Exit status |
|---|---|---|
| Output is not a terminal | The page once, plain text without colour codes, and `look only: run arch-update in a terminal to update` if anything waits or could not be read. No key is read. | 0 |
| Every queue was read and all are empty (not with `auto --ask`) | `TOTAL    nothing waiting`, no question. | 0 |
| The repository queue could not be read (even with `auto --ask`) | `nothing is asked while the queue cannot be read; by hand: checkupdates` if `checkupdates` is installed and failed, `by hand: sudo pacman -S pacman-contrib` if it is missing. | 0 (20 in the weekly window) |
| Every waiting row is an AUR row, `unattended_aur` is not `true`, and no queue was unread | `nothing for the update to do; by hand: yay -Sua` | 0 |

If AUR rows wait together with other rows, or a Flatpak or Snap queue could not
be read, the question is asked.

### Keys

| Key | Action |
|---|---|
| `y` | Update now. See below. |
| `l` | Show `last run <id>` and the tail of that run's `run.log`, colour codes removed. A run that stopped at a check has a report but no log: then it shows `last run <id>: no commands ran, so there is no log`, the headline and the lines of that report. `no run on record yet` if there is none. Any key returns. |
| `k` | List the kernel profiles. A number selects one; after the confirmation `Install kernel profile <id> and set it as the GRUB default? [y/N]` it is installed, as with `arch-update kernel`. Enter goes back. Errors (including an unreadable GRUB file) are shown on screen, not as a traceback. |
| a bare Esc, Enter, `n`, `q`, any other single key | Not now. Nothing is changed and the screen says `not now. I will ask again next time; by hand: arch-update`. |
| a key that sends several bytes (arrows, Home, F-keys, Alt-keys) | Ignored. It is read whole, so it is neither `y` nor "not now", and the wait goes on. |
| Ctrl-D, or the end of the input | Not an answer. The screen says `no input: standard input ended before an answer; nothing was changed; by hand: arch-update` and the exit status is 70. |

A key is read without Enter on a terminal. If stdin is not a terminal, one line
is read and its first character is the key; an empty line is "not now".

The question waits `ask_timeout_minutes` (default 30). With no key in that time
the screen says `no answer within N minutes: skipped this week` and the exit
status is 80. This applies to a visit started by hand as well as to the timer's;
only the timer's window (`--record`) writes `ask-result.json`, so by hand nothing
is written.

### What `y` does

1. At a terminal (stdin and stdout), the update's commands run in the terminal's
   own foreground session, whether the password is needed or not; they get
   Ctrl-C from the terminal like any command typed there. Then, if `sudo -n true`
   fails, the tool runs `sudo -v` on the terminal so you can type the password
   once. It does not read, keep or log it. If sudo does not accept the password,
   or there is no terminal, the screen says why and nothing is changed; the exit
   status is 70. Without a terminal the line ends `Fix: arch-update auto
   --sudoers`. With the sudoers rule installed no password is asked.
2. Starts a thread that runs `sudo -n -v` once a minute while the update runs,
   to renew sudo's ticket, so the stores that run after the first (Snap, the AUR
   with `--sudoflags -n`) still find root. It starts only at a terminal and only
   if `sudo -n -v` is accepted at the start (a ticket exists; with rules alone
   there is nothing to renew). If a renewal is refused, the renewals stop and the
   result block ends with a red line `sudo ticket expired during the update; run
   arch-update again and type the password (unattended: arch-update auto
   --sudoers)`. The thread stops on every way out of the update.
3. Runs the same update as `arch-update auto`: see
   [Sequence of the update](#sequence-of-the-update). The screen replaces the
   table with one line per step: `checking`, `failure`, `record`, `updating`. The
   `N changed` on an `updating` line counts packages by name: one upgrade is 1,
   not the two lines of a package listing that differ.
4. If another update holds `auto.lock` (the weekly unit, or a second Update Day),
   nothing runs: `another update is running; nothing was changed`, exit status 80.
5. Reads the queue again and ends with one block: what changed per store and
   per package (`name  old → new`); `no new versions` if the run was clean and
   changed nothing, `nothing was changed` if it failed and changed nothing;
   every problem in red as `<check>: <cause>` and `fix: <command>`, where the
   cause is the last line of the command's output that has words in it, not the
   `[exit N]` line the log adds; `AUR waits for you; by hand: yay -Sua`
   if the AUR was held; a line for each package changed in the AUR too recently;
   `new config version: <file>  compare: sudo pacdiff` for each `.pacnew`; and
   the headline `DONE` or `FAILED`, with ` · REBOOT NEEDED` if a kernel was
   replaced. Enter closes. Ctrl-C at this prompt only closes: the update is over
   and is not reported as stopped.

### Interrupts

Ctrl-C, SIGTERM and SIGHUP never leave the screen half drawn: the cursor and the
signal handlers are put back, and one red line says what happened. The exit
status is 128 plus the signal number: 130 (SIGINT, Ctrl-C), 129 (SIGHUP) or 143
(SIGTERM).

| When | What happens | Exit status |
|---|---|---|
| At the question, or while the page is being read | `interrupted; nothing was changed`. Nothing was run. | 130, 129 or 143 |
| Ctrl-C during the update | The package command in progress receives the Ctrl-C from the terminal and decides how to stop (pacman and yay finish or undo the step). This tool never kills it: it waits for it to end; a second Ctrl-C is passed on and the wait goes on. Then nothing new starts, the lock is released, and the report is written (see below). | 130 |
| SIGHUP during the update (`kill -HUP`, or a closed terminal) | The same as Ctrl-C. A closed terminal also hangs up on the foreground session, so the commands in it receive SIGHUP from the kernel; `kill -HUP` sent to this program alone does not reach them. | 129 |
| SIGTERM during the update | Not passed to the command: the one in progress is left alone and finishes on its own, no further store starts (this is also checked between stores), and the lock is released. | 143 |

The report of an interrupted run has `state` `INTERRUPTED`, `exit_code` 130, 129
or 143, `signal` (the number of the signal) and `child_stopped`: `true` if the
command that was running ended by a signal (judged by how it ended: it was sent
one from here, or its status is a signal's, which is killed by it, 128 plus the
number, or the bare number pacman exits with), `false` if it ended on its own,
`null` if none was running. Its trouble list gets an entry with `"probe": "run"`
and the case `INTERRUPTED`, whose `fix` is `arch-update`; `detail` is `stopped
with Ctrl-C` or `stopped by SIGHUP` or `stopped by SIGTERM`. The red line on the
screen depends on what is known:

| Known | The line |
|---|---|
| Nothing had started | `interrupted; nothing was changed` |
| `child_stopped` is `true` | `interrupted; the update was stopped, run arch-update again` |
| `child_stopped` is `false` or `null` | `interrupted; the update may have finished — run arch-update to check` |

`arch-update auto` outside Update Day has no terminal session of its own to share:
its commands run in a session of their own, and on Ctrl-C this tool sends SIGINT
to that session and then waits, as above. It catches Ctrl-C and SIGTERM; SIGHUP
is caught only by Update Day.

### Display

- A frame is never taller than the terminal: it has at most one line fewer than
  the terminal has rows. When the page is too tall, these go first, in this
  order: the `l`/`k` footer, the blank lines between blocks, the notes under
  `PENDING`, then the table down to its header, one row and `+ N more` (the
  story of the run, to as few lines as needed, down to none), and last the
  `MACHINE` and `PENDING` blocks. What is left is the header, `TOTAL` and the
  question.
- Colour is used only when the output is a terminal, `TERM` is not `dumb` and
  `NO_COLOR` is not set. Cursor moves, screen clears, the hidden cursor and
  synchronized updates are written only to a terminal that is not `dumb`. A
  `dumb` terminal gets plain frames, printed when the question or the ending
  changes, and is never redrawn. A pipe gets plain words.
- A resize redraws the page at the new size.
- The `.desktop` entry starts the program as `env ARCH_UPDATE_HOLD=1`. When
  that variable is set and you are at a terminal, a visit that ends without an
  update (nothing waiting, no question, not now, a refused password, another
  update running, an interrupt) adds `[Enter] close` and waits for Enter, because
  the window would vanish with the program. An update always waits for Enter at
  its end. A timeout and a closed input do not wait.

`arch-update auto --ask` is the same screen, but asks even when nothing waits.

## Commands in detail

| Command | Detail |
|---|---|
| (none) | Update Day, above. |
| `status [--json]` | Prints the OS, update counts (repository from `checkupdates`, AUR from `yay -Qua`, or `paru -Qua` if `yay` is not installed; `?` with a note when unreadable), running kernel and whether its modules are present, bootloader, default kernel package, free space on `/`, pacman lock, GPU, each installed kernel with its boot files and headers, failed units, `.pacnew`/`.pacsave` count and the Hyprland library state. `--json` prints the raw snapshot. |
| `attest` | Prints as JSON whether kernel, initramfs, DKMS, failed units and the boot loader entry are in order, whether a reboot is pending, and the `.pacnew` inventory. |
| `kernel [profile] [--dry-run] [--yes]` | Without a profile, prints the detected kernel and the profiles as JSON. With one, asks `Install kernel profile <id> and set it as the GRUB default? [y/N]` (at a terminal; without one, `--yes` is required, otherwise exit 1), then installs the kernel and, with GRUB, selects it. `--dry-run` prints what would change and does not ask. `auto` keeps the detected kernel and asks nothing. |
| `auto [--dry-run] [--status] [--init] [--ask] [--sudoers [--aur]]` | The update without questions. With no flag it runs and prints the JSON report. `--dry-run`: see [Dry run output](#dry-run-output). `--status`: the problems left by the last run, held stores, `.pacnew` files and a pending reboot, each with its `fix`. `--init`: write the starter configuration; an existing file is not touched. `--ask`: Update Day, asking even when nothing waits. `--sudoers`: print the sudoers rules; `--aur` adds the AUR rules. Flags are examined in the order `--status`/`--init`, `--ask`, `--sudoers`, then the run. Ctrl-C during the run ends it as in [Interrupts](#interrupts) (exit 130). |
| `schedule [--install-auto] [--install] [--remove-auto] [--remove]` | Without flags, prints the state of both timers as JSON. `--install-auto` and `--install` write the unit files and enable the timer (safe to repeat). `--remove-auto` and `--remove` stop a timer and delete its unit files; removing what is not there succeeds. Exit 1 if systemd refuses. If several flags are given, the first of `--install-auto`, `--install`, `--remove-auto`/`--remove` wins. |
| `patrol` | Runs the [patrol](#daily-patrol) now and prints its report. |
| `share-report` | Prints a sanitised summary of the last patrol as JSON (schema `arch-update-share/v1`: health, reboot need, pending counts, CVE count, the names of the issue kinds). It writes nothing and sends nothing. Without a patrol report it exits 1 with `run arch-update patrol first`. |
| `vault [--classify TEXT]` | Prints the shipped catalogue of known failures as JSON, or only the entries whose pattern matches `TEXT`. |
| `cve` | Prints the `arch-audit` findings as JSON (`available`, `findings`, `count`, `error`). Exit 0 even when `arch-audit` is missing. |
| `scan-sessions [--output-dir DIR] [--json]` | See [Session scanner](#session-scanner). |
| `--version` | Prints the version. |

`auto` passes `--noconfirm` to `pacman`, `yay` and `paru`, and `-y
--noninteractive` to `flatpak`.

Two flags of `auto` do not appear in `--help`. The timer's service runs
`arch-update auto --scheduled`, which asks first if `"ask": true` and otherwise
updates. The window it opens runs `arch-update auto --ask --record`; `--record`
makes the visit write its answer to `ask-result.json` and exit with the status
the timer needs (see [Schedule and ask mode](#schedule-and-ask-mode)).

Other commands exit 0 on success and 1 on an error or a refusal, including an
unreadable file (2 for a usage error, 130 after Ctrl-C).

### Removed in 0.18.0

The looping menu and its helpers are gone: the commands `menu`, `plan`, `run`,
`preview`, `preview-pkg`, `preview-pick`, `snapshot-json`, `search` and
`store-search`, the `fzf` and `gum` dependencies, and the `update_modes` entry
in the profile file. `arch-update` with no command opens Update Day instead.

## Safety statements in detail

- No reboot: no code path runs `reboot`, `shutdown` or `poweroff`. A pending
  reboot means the module directory of the running kernel is gone
  (`engine.detect_kernel`, `watch.probe_machine`); `auto` then exits 10 and
  Update Day adds `REBOOT NEEDED`. Installing a kernel with `k` or `kernel`
  says `reboot needed to boot into it; this tool does not reboot`.
- `.pacnew` and `.pacsave` files are listed with `pacdiff -o`. The result screen
  and `auto --status` print `sudo pacdiff` as the command for you to run. No
  code reads or writes them.
- The pacman lock is only tested with `Path.exists()`. A lock left by a killed
  package manager is waited on (up to three times two minutes) and never
  removed; the run exits 30.
- The update runs `pacman -Syu --noconfirm` and does not fetch the Arch news. A
  conflict that pacman cannot settle with `--noconfirm` makes the store fail
  (for example `PACMAN-REPLACEMENT-CONFLICT`) and is reported with its `fix`.
- No shutdown or sleep inhibitor is taken. A logout, suspend or power-off during
  an update is not guarded. SIGTERM during an update lets the command in
  progress finish and starts nothing new; Ctrl-C and SIGHUP never kill it either
  (see [Interrupts](#interrupts)).
- The record written before an update has no restore command.
- Kernel selection installs the package with `pacman -S --needed`. With GRUB it
  rewrites `GRUB_DEFAULT`, after copying `/etc/default/grub` to
  `/etc/default/grub.arch-updater-<time>.bak`. If installing the file or
  regenerating `grub.cfg` fails, or the new `grub.cfg` does not mention the
  kernel, it copies the backup back and regenerates `grub.cfg`, and the error
  says how far that got: `configuration was rolled back`; or `<file> was
  restored from <backup>, but regenerating <grub.cfg> failed (exit N); run: sudo
  grub-mkconfig -o <grub.cfg>`; or `the rollback FAILED too (cp exit N): <file>
  may still be changed; the backup is <backup>`. If the backup itself fails, GRUB
  is left untouched. If `/etc/default/grub` or `grub.cfg` cannot be read, the
  error says `kernel <package> is installed, but GRUB could not be read (...);
  GRUB was left untouched` (before any change) or `GRUB was rewritten, but
  <grub.cfg> could not be read to check it (...); the backup is <backup>`. With
  another boot loader only the package is installed. The backups are never
  cleaned up.
- Failed systemd units present at the first check of a run are not attributed
  to the update.
- Package and boot-file transactions never get a time limit
  (`shell.TRANSACTION_TOOLS`: `pacman`, `yay`, `paru`, `paccache`, `mkinitcpio`,
  `dkms`, `snap`, `flatpak`, `grub-mkconfig`). The checks, the queue reads and
  the `tar` of `/etc` (600 s) do.
- A sudoers `*` appears in the generated rules only with `--aur`.

## Free space on `/`

The disk check fails below 6 GiB free (`SOFT_FREE_BYTES` in
`arch_updater/engine.py`, `probe_disk` in `arch_updater/watch.py`). The repair
for it runs `sudo -n paccache -rk2`, which deletes every cached package file
except the two newest versions of each package, and then checks again. If space
is still low after three repairs, the run stops before any update and exits 30.
The same limit and the same cleanup apply to `y` on Update Day and to the
unattended run. The patrol does not check free space. `ROOT-SPACE-LOW` is also the case for a pacman error such as `No
space left on device`.

## AUR in detail

- With `"unattended_aur": false` (default) the update skips the AUR and reports
  it under `held` (`unattended AUR is off; run yay -Sua`,
  as `auto --status` and the notification print it). Update Day shows `by hand:
  yay -Sua`. Update it with `yay -Sua`, or click the notification.
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
- Without the sudoers rules, Update Day asks for the password once (`sudo -v`)
  and every later command uses `sudo -n` on sudo's ticket. While the update runs
  the tool renews that ticket once a minute (`sudo -n -v`, from
  `arch_updater/keepalive.py`; it starts only at a terminal when a ticket exists,
  and stops on every way out), so a long build does not outlive it. The case
  `SUDO-TIMESTAMP-EXPIRED` (the output matches `a password is required`; its
  `fix` writes the sudoers rules through `visudo -cf`) can then arise on Update
  Day only if a renewal is refused; the ending says `sudo ticket expired during
  the update; run arch-update again and type the password (unattended:
  arch-update auto --sudoers)`. The unattended run has no terminal and no ticket:
  it needs the rules.

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
commands with a password. Without the rules, Update Day asks for the password
once on `y`; the timer's `auto` cannot, and exits 70.

## Sequence of the update

`y` on Update Day, `arch-update auto` and the timer run the same sequence.

1. **Check.** Nine checks run concurrently. They change nothing, except that the
   commands you configure as conditions and smoke tests run as written. The
   screen's `MACHINE` line is one such pass kept in memory (see
   [The look](#the-look)).

   | Check | Screen name | Fails or blocks when | Exit |
   |---|---|---|---|
   | root access | `root access` | running as root, or `sudo -n true` fails | 70 |
   | conditions | `config`, or the name you gave | no configuration file (70); a configured command exits non-zero (its `exit`, default 20) | 70, 20, 40, 50 |
   | record directory | `backup record` | `keep` is set and the nearest existing folder on its path is not writable or is on the same device as `/` | 50 |
   | pacman lock | `pacman is busy` | `/var/lib/pacman/db.lck` exists | 30 |
   | disk space | `disk space` | less than 6 GiB free on `/` | 30 |
   | name resolution | `network` | `archlinux.org` does not resolve | 30 |
   | package database | `package database` | `pacman -Dk` exits non-zero | 20 |
   | kernel and services | `kernel and services` | kernel image or initramfs missing; GRUB config without the default kernel; NVIDIA DKMS module missing for an installed kernel (only if `nvidia-dkms` or `nvidia-open-dkms` is installed); a unit that failed after the first check of this run; a Hyprland binary with missing libraries | 30 |
   | programs | `programs` | a configured `smoke` command exits non-zero | 30 |

   Units that were already failed at the first check of the run are not blamed
   on the update. A check that raises an error counts as "unknown" and blocks
   the run with exit 20. The exit column applies when nothing else decides the
   exit first; see [Exit status](#exit-status).
2. **Update.** Each installed store (`pacman`, then `yay` or `paru`, `flatpak`,
   `snap`) is updated once. A failing store does not stop the others. Before the
   first update the record is written: `packages.txt`, `explicit.txt`,
   `foreign.txt` and `etc.tar.zst` (see `keep`). If the record cannot be
   written, nothing is updated (exit 50). A store counts as having changed
   something if its package listing differs afterwards; the count is of package
   names, so one upgrade is 1.
3. **Repair.** If a store failed, its failure is looked up in the catalogue of
   known failures. If a repair is recorded for it, the repair runs and the
   sequence returns to step 1. After the checks pass, a store that failed with
   a repairable failure is run again; a store whose failure has no repair is
   not run again. A failed check is looked up the same way. At most three
   repairs run per update.
4. The run ends when the checks pass and no store is left to update. It also
   ends, with the failure reported, when a failure has no recorded repair, when
   the repair could not be attempted, or when the three repairs are spent. It
   ends as well when a check is blocked (database check, conditions, root
   access, record directory) or a check itself failed to run. If that happens at
   the first check, nothing has been updated. Ctrl-C, or SIGHUP on Update Day, ends
it as well, after the running command has ended (see
[Interrupts](#interrupts)).

After a repair the checks always run again before any store is updated. A repair
reports only whether it was attempted; its exit status is ignored. A repair that
cannot be attempted ends the run: `quarantine-aur-cache` when nothing was moved,
`rebuild-against-new-libraries` when `unattended_aur` is off or `yay` is
missing.

The sequence is the program in `arch_updater/control.tis`, executed by
`arch_updater/tis.py`. A node is limited to 15 instructions and literals to
-999..999.

## Catalogue of known failures

`arch_updater/data/failure-modes.json` is the shipped catalogue (installed under
`~/.local/share/arch-update-deck/arch_updater/data/`). An entry has an `id`, a
`match` (a regular expression searched in the failing command's output), a
`meaning`, a `fix` and optionally a `repair`. The `fix` is one command to run by
hand, on one line, readable by `sh`, with no placeholder. It is never the command
that just failed: no `fix` is `pacman -Syu`, `yay -Sua`, `paru -Sua`,
`flatpak update`, `snap refresh` or `arch-update auto` (with or without `sudo`),
because repeating the failed update would fail the same way. A test pins this.
Where the failure is in the output of a run, the `fix` reads or searches the
latest log instead (see the table below). An entry with `"classify_output": false` is never matched against command
output; the checks raise it themselves. The `repair` is one of these names
(`arch_updater/repairs.py`); a name not in this list is ignored, and the failure
is not repaired:

| `repair` | What it does |
|---|---|
| `wait-for-lock` | Waits up to 2 minutes (24 checks, 5 s apart) for the pacman lock to disappear. Never removes it. |
| `trim-cache` | `sudo -n paccache -rk2`. |
| `wait-for-dns` | `resolvectl flush-caches`, then waits up to 60 s for `archlinux.org` to resolve. |
| `wait-for-mirror` | Waits 60 s. |
| `quarantine-aur-cache` | Renames a `yay` cache directory that has no `PKGBUILD` to `<name>.quarantine-<run id>` in the same directory. Only a real directory directly inside the cache, named in yay's "error downloading sources" line. Nothing is deleted. |
| `rebuild-initramfs` | `sudo -n mkinitcpio -P`. |
| `rebuild-dkms` | `sudo -n dkms autoinstall` (for the running kernel; a kernel installed later is built by its pacman hook). |
| `rebuild-against-new-libraries` | `yay -S --rebuild` of the `rebuild` packages named in `smoke`. Only with `unattended_aur` on and `yay` installed. |

The update acts on `repair` only. The `stage`, `response`, `automatic` and
`observed_in` fields in the shipped file are shown by `vault` and read by
nothing else; `response` is prose for a reader. In the shipped file `automatic` is true exactly for the entries
that have a `repair`.

The shipped cases (`arch-update vault` prints them with their `meaning`):

| Case | `repair` | `fix` |
|---|---|---|
| `ROOT-SPACE-LOW` | `trim-cache` | `sudo paccache -rk2 && df -h /` |
| `PACMAN-REPLACEMENT-CONFLICT` | - | `grep -h "in conflict" ~/.local/state/arch-updater/runs/*/run.log \| tail -n 3` |
| `MIRROR-STALE-404` | `wait-for-mirror` | `sudo pacman -Syyu` |
| `AUR-CACHE-UPSTREAM-MISSING` | - | `less +G "$(ls -td ~/.local/state/arch-updater/runs/*/ \| head -n 1)run.log"` |
| `AUR-CACHE-PKGBUILD-MISSING` | `quarantine-aur-cache` | `grep -h "error downloading sources" ~/.local/state/arch-updater/runs/*/run.log \| tail -n 1` |
| `AUR-SOURCE-REMOTE-PREDICATE` | - | `less +G "$(ls -td ~/.local/state/arch-updater/runs/*/ \| head -n 1)run.log"` |
| `AUR-GIT-LFS-PROTOCOL` | - | `sudo pacman -S --needed git-lfs` |
| `SUDO-TIMESTAMP-EXPIRED` | - | `sh -c 'f=$(mktemp) && arch-update auto --sudoers > "$f" && visudo -cf "$f" && sudo install -m 0440 "$f" /etc/sudoers.d/zzz-arch-update-auto; rm -f "$f"'` |
| `AUR-SPLIT-PACKAGE-GRANULARITY` | - | `less +G "$(ls -td ~/.local/state/arch-updater/runs/*/ \| head -n 1)run.log"` |
| `AUR-BUILD-LAYER-FAILURE` | - | `less +G "$(ls -td ~/.local/state/arch-updater/runs/*/ \| head -n 1)run.log"` |
| `DKMS-PARTIAL-MODULE-FAILURE` | `rebuild-dkms` | `sudo dkms autoinstall && dkms status` |
| `HYPRLAND-ABI-FORK` | - | `arch-update attest` |
| `RUNNING-KERNEL-MODULES-MISSING` | - | `systemctl reboot` |
| `PACNEW-PENDING` | - | `sudo pacdiff` |
| `NEW-FAILED-UNIT` | - | `systemctl --failed; systemctl --user --failed` |
| `KERNEL-BOOT-ARTIFACT-MISSING` | `rebuild-initramfs` | `sudo mkinitcpio -P` |
| `STALE-BOOTLOADER-CONFIG` | - | `sudo grub-mkconfig -o /boot/grub/grub.cfg` |
| `PACMAN-LOCK` | `wait-for-lock` | `sudo fuser -v /var/lib/pacman/db.lck` |
| `DNS-FAILED` | `wait-for-dns` | `resolvectl status` |
| `ABI-MISMATCH` | `rebuild-against-new-libraries` | `pacman -Qm` |
| `AUR-UNREACHABLE` | `wait-for-mirror` | `curl -sI https://aur.archlinux.org/rpc/v5/info` |
| `PACMAN-DATABASE` | - | `pacman -Dk` |
| `KEEP-UNWRITABLE` | - | `arch-update auto --dry-run` |
| `CONDITION` | - | `arch-update auto --dry-run` |
| `NOT-CONFIGURED` | - | `arch-update auto --init` |
| `NO-ROOT` | - | `sh -c 'f=$(mktemp) && arch-update auto --sudoers > "$f" && visudo -cf "$f" && sudo install -m 0440 "$f" /etc/sudoers.d/zzz-arch-update-auto; rm -f "$f"'` |
| `INTERRUPTED` | - | `arch-update` |

`NO-ROOT` and `SUDO-TIMESTAMP-EXPIRED` share the sudoers fix: it writes the rules
to a temporary file, checks it with `visudo -cf` and only then installs it, so
rules that were never checked are not written to `/etc`.

When a store fails and no entry matches its output, the failure is added to
`~/.local/state/arch-updater/census.json` as a new entry `LOCAL-<hash>`. Its
`match` is the line that identified the failure (the last line containing
"error", else the last line) with numbers loosened. Its `fix` is
`less +G '<path of run.log>'`, the path quoted for the shell, its `meaning` says when and where it was first seen, and
`seen` counts how often it came back. It is recognised on later runs. It has no
`repair`, so the store is not retried. To have the failure handled, add a `fix`
or a `repair` to the entry in `census.json`; do not edit the installed
catalogue, which `install.sh` overwrites. Only failures of the stores (pacman,
AUR, Flatpak, Snap) are recorded this way. A failed check is never added to the
census.

## Schedule and ask mode

```bash
arch-update schedule --install-auto
```

Writes `arch-update-auto.service` and `arch-update-auto.timer` to
`~/.config/systemd/user/` and enables the timer. It runs Saturday 12:00 local
time, with up to 30 minutes of random delay, and the timer has
`Persistent=true`: after a missed Saturday (the machine was off, or you were
logged out) the run starts when your user manager next starts, at login or, with
lingering, at boot. The service runs `arch-update auto --scheduled` with
`SuccessExitStatus=10 80`, `KillMode=mixed` and `TimeoutStopSec=150min`, so any
other exit status leaves the unit failed and visible in
`systemctl --user --failed`. Its `PATH` is `%h/.local/bin:%h/bin:
/usr/local/sbin:/usr/local/bin:/usr/bin`. Without `"ask"` the unit's status is
the status of `arch-update auto`.

With `"ask": true` the timer opens a terminal on `arch-update auto --ask
--record`: the [Update Day](#update-day) screen, with the question asked even if
nothing waits. `--record` makes the visit write its answer to `ask-result.json`
and exit with the status the unit needs. In kitty the window is titled
`update day` with a black background and 118 by 36 cells. There the keys, the
password prompt and the timeout are those of Update Day. Because `y` can ask for
the password in that window, the sudoers rule is not needed for this mode.

Exit status of the scheduled run (`day.scheduled`, `day.ask`). The unit's
`SuccessExitStatus` is `10 80`; any other status leaves it failed.

| What happened | Status | `outcome` in `ask-result.json` |
|---|---|---|
| The update ran (after `y`, or because no terminal was found and it ran unasked) | Its own status, as in [Exit status](#exit-status). | `updated` (none when it ran unasked) |
| A terminal was found, but neither `DISPLAY` nor `WAYLAND_DISPLAY` is set | 20 (BLOCKED), plus a notification and a line in `ask-window.log`. | none |
| The answer was "not now" | 80 (SKIPPED) | `declined` |
| No answer within `ask_timeout_minutes` | 80 | `timed-out` |
| The repository queue could not be read, so nothing was asked | 20 (BLOCKED) | `unread` |
| Only AUR packages wait and the AUR is off, so nothing was asked | 0 | `nothing-to-do` |
| `y`, but sudo needs a password and none was accepted | 70 (TOOL_FAILURE) | `no-sudo` |
| The input ended (Ctrl-D) before an answer | 70 | `no-input` |
| Another update holds `auto.lock` | 80 | `busy` |
| Ctrl-C, SIGTERM or SIGHUP, at the question or during the update | 128 plus the signal: 130, 143 or 129 | `interrupted` |
| The window closed and left no answer on record (for example the program was killed) | 70, plus a notification and a line in `ask-window.log`. | none |

If you close the window while the update is running, the terminal hangs up on
its foreground session: the update's commands run in it, so pacman receives
SIGHUP from the kernel, and this program receives it too. It waits for the
command in progress to end, writes the report (`INTERRUPTED`, `signal` 1), and
records `interrupted` with exit code 129 in `ask-result.json` (the file is not
written to the terminal, so the closed window does not matter); the timer's run
then exits 129 and the unit shows as failed. How pacman answers SIGHUP is up to
it, and a real closed window is not tested here: the behaviour above is what the
code does when the signal arrives. A signal sent to this program alone (for
example `kill -TERM` on its process id) is not passed to the command: it is left
alone and finishes on its own, and the recorded reason is `interrupted; the
update may have finished — run arch-update to check`. The terminal emulator's own
exit status is never used.

`ask-result.json` holds `at`, `outcome`, `exit_code` and `reason`. `outcome` is
one of `updated`, `declined`, `timed-out`, `no-sudo`, `no-input`, `busy`,
`unread`, `nothing-to-do` and `interrupted`; the table above says when. Only the
weekly window (`--record`) writes it; a visit started by hand writes none. The
timer's run deletes it before opening the window and reads it afterwards.

Requirements of ask mode:

- A terminal must be found: `$TERMINAL` if it is set and in `PATH`, otherwise
  the first of the list below. A `TERMINAL` that is not in the list is started
  with `-e`. If no terminal is found, the update runs without asking. The unit
  reads the answer when the window's process ends, so each terminal is started
  in a form that stays in the foreground until the command in it has ended:

  | Terminal | Command line |
  |---|---|
  | `xdg-terminal-exec` | `xdg-terminal-exec <command>` |
  | `kitty` | `kitty <command>` (with the window options above) |
  | `foot` | `foot <command>` (not `footclient`) |
  | `wezterm` | `wezterm start --always-new-process -- <command>` |
  | `alacritty` | `alacritty -e <command>` |
  | `konsole` | `konsole --nofork -e <command>` |
  | `gnome-terminal` | `gnome-terminal --wait -- <command>` |
  | `xterm` | `xterm -e <command>` |
- The terminal is started from the systemd user manager, so that manager's
  environment must contain `DISPLAY` or `WAYLAND_DISPLAY`. The service sets only
  `PATH`; most desktop sessions import the display variables, otherwise run
  `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY`. `TERMINAL` is
  likewise only seen if it is in that environment. If neither display variable
  is set, the weekly update is not run; a notification and a line in
  `ask-window.log` say so.
- The window's output, and these failure messages, go to `ask-window.log` in
  the state directory.

## Results

If `notify-send` exists, every update that is not a dry run sends a desktop
notification, including one started with `y`: the state and the problems, each
with its `fix`. A skipped run (status 80) sends one too. Urgency is critical
unless the status is 0, 10, 80 or 130.

Clicking the notification opens a terminal on the `fix` of the first problem
that has one, or on `yay -Sua` if only the AUR was held. This needs a terminal
found as described above, `systemd-run`, and a `notify-send` that supports
`--wait` and `--action`. The `fix` runs as your user; those that start with
`sudo` ask for your password in that terminal.

`arch-update auto --status` prints the problems left by the last run, each with
its `fix`, plus held stores, `.pacnew` files and a pending reboot. The same
problems stand under `PENDING` on Update Day.

## Daily patrol

```bash
arch-update schedule --install
```

Enables a daily read-only job at 09:30, with up to 15 minutes of random delay
and `Persistent=true`. It writes `arch-update-patrol.service` and
`arch-update-patrol.timer` to `~/.config/systemd/user/`; the service runs
`arch-update patrol`. To stop it: `arch-update schedule --remove`.

The patrol runs read-only queries: `pacman -Q` for the kernel packages,
`systemctl --failed`, `pacdiff -o`, `dkms status`, `checkupdates`, the AUR
queue (`yay -Qua`, or `paru -Qua` if `yay` is not installed; the same reader as
Update Day and `status`) and `arch-audit`, each only if the program is
installed (a few more apply when Hyprland or an NVIDIA driver is installed). It never installs anything and never
uses `sudo`. `checkupdates` downloads the package databases into a temporary
copy (`${TMPDIR:-/tmp}/checkup-db-<uid>`), not into `/var/lib/pacman`; the AUR
query and `arch-audit` use the network.

It writes `last-patrol.json` and appends to `patrol-history.json`, which keeps
the last 12 patrols. `arch-update share-report` reads `last-patrol.json`.

## Session scanner

`arch-update scan-sessions` reads local Codex session files
(`~/.codex/sessions/`) and Claude Code session files for the home directory
(`~/.claude/projects/<home path with dashes>/`), and `/var/log/pacman.log`, to
find sessions in which a package-manager command changed the system. It runs
only when you ask for it. It writes `update-sessions.json`,
`UPDATE-SESSIONS.md` and `failure-evidence.json` to `--output-dir` (default
`reports/` in the state directory), prints the Markdown report and a line
`Proven sessions: N` (with `--json`, the JSON report instead). In the commands
and evidence lines it writes, secrets that look like `TOKEN=...`, passwords and
`Authorization` headers are replaced. Nothing leaves the machine: the scanner
makes no network call.

## Configuration

`~/.config/arch-updater/auto.json` (`$XDG_CONFIG_HOME/arch-updater/auto.json`).
Plain `arch-update` writes the starter file if it is missing; so does
`arch-update auto --init`. An existing file is never overwritten. Without this
file `arch-update auto` exits 70.

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
| `unattended_aur` | `true` builds and installs AUR updates without asking. Default `false`. Only the JSON value `true` counts. See [AUR](../README.md#aur). |
| `aur_min_age_days` | With `unattended_aur`: leave out packages whose AUR entry changed within this many days. Default 4. |
| `ask` | `true` makes the timer open Update Day in a terminal before the weekly update. Default `false`. Read only by the timer's run. See [Schedule and ask mode](#schedule-and-ask-mode). |
| `ask_timeout_minutes` | How long the question waits for an answer, on the timer and by hand. A number greater than 0; anything else falls back to 30. |

The check that `keep` is "another disk" compares the device number of `keep`
with that of `/`. Two subvolumes of one btrfs filesystem have different device
numbers, so a second subvolume on the same physical disk passes the check.
Choose a directory on a separate physical disk yourself. Without `keep`, the
record is written to the system disk and the check passes.

## Exit status

**Update Day** (`arch-update`, `arch-update auto --ask`; `--record`, the weekly
window, differs where noted):

| Status | When |
|---|---|
| 0 | Nothing waiting; output is not a terminal; no question was asked (repository queue unread, or only AUR rows waiting); answered not now; or the update ended with `DONE` and status 0. |
| 10 | The update ended with `DONE · REBOOT NEEDED`. |
| 20, 30, 40, 50 | The update ended with `FAILED`; the number is the update's own status in the table below. |
| 70 | `y`, but sudo needs a password and it was not accepted or there is no terminal (nothing was changed); or the input ended before an answer; or the updater itself broke. |
| 80 | No answer within `ask_timeout_minutes`; or another update holds `auto.lock` (nothing was changed). |
| 130 | Ctrl-C, at the question or during the update. |
| 129 | SIGHUP, at the question or during the update. |
| 143 | SIGTERM, at the question or during the update. |

With `--record`, "not now" is 80 and an unread repository queue is 20; the status
is also written to `ask-result.json` (see
[Schedule and ask mode](#schedule-and-ask-mode)). By hand, "not now" is 0 and
no file is written.

**`arch-update auto`** exits with these statuses (`arch_updater/exits.py`,
`exit_code` in `arch_updater/auto.py`). A dry run uses the same numbers.

| Status | Name | Meaning | A script should |
|---|---|---|---|
| 0 | IDLE | The last check passed and nothing is left to update. Updates may have been installed. | Nothing. |
| 10 | READY_FOR_REBOOT | As 0, and the module directory of the running kernel is gone, which means a kernel was replaced. | Schedule a reboot yourself; the tool never does. |
| 20 | BLOCKED | The run stopped on a "blocked" or "unknown" result with no more specific status: a condition with `exit` 20 or without a valid `exit`, a failed `pacman -Dk`, or a check that crashed. | Read `auto --status`, fix the cause, run again. |
| 30 | RECOVERY_PENDING | A store failed and is still failed, or a check failed (lock, low space, name resolution, kernel and services, programs) and no repair is recorded, none could be attempted, or the three repairs are spent. A failed store decides the status even if a later check was blocked with 40 or 50, unless the failure is in the record directory (50). | Read `auto --status`, run its `fix`, then run again. Do not repeat the run blindly. |
| 40 | NETWORK_BLOCKED | A condition configured with `"exit": 40` failed. | Try again later. |
| 50 | BACKUP_BLOCKED | A condition configured with `"exit": 50` failed, or the record directory is unusable, or the record could not be written. | If it comes from a condition, try again later. Otherwise fix the directory. |
| 70 | TOOL_FAILURE | No configuration file, no passwordless root (or running as root), the tool itself crashed, or its report could not be written. | Fix the setup. Retrying does not help. |
| 80 | SKIPPED | Another non-dry run holds `auto.lock`, or (weekly ask window) the question got "not now" or timed out. | The update did NOT happen. If another run holds the lock, that run is the one that counts; if the question was declined, run it yourself when you want it. |
| 129, 130, 143 | INTERRUPTED | A signal ended the run: SIGHUP 129 (Update Day only), Ctrl-C 130, SIGTERM 143. The command in progress was let finish (never killed) and nothing else started; the lock is released and the report is written with `state` `INTERRUPTED`, `signal` and `child_stopped` (see [Interrupts](#interrupts)). | The update may be half done, or it may have finished: run `arch-update` (or `arch-update auto`) again; both start with the checks and are safe to repeat. `auto --status` shows the case `INTERRUPTED` until a run ends without one. Do not count it as a failure of the update itself. |

Other commands exit 0 on success and 1 on an error or a refusal (2 for a usage
error, 130 after Ctrl-C).

### A lock that never clears

A pacman lock that does not go away is what a real stale lock looks like: the
run waits three times for two minutes and exits 30. Update Day shows the
headline `FAILED` and the command to inspect the lock
(`sudo fuser -v /var/lib/pacman/db.lck`); everywhere else, including
`report.json` and `auto --status`, the state is `RECOVERY_PENDING`. The tool
never removes the lock.

## Files

State directory: `~/.local/state/arch-updater/`, or `$ARCH_UPDATER_STATE`.

| Path | Content |
|---|---|
| `~/.config/arch-updater/auto.json` | Configuration. |
| `runs/auto-<time>/run.log` | Output of the commands the update executed through its command wrapper: the store updates, repairs, the `tar` of `/etc`. The probes (`sudo -n true`, `pacman -Dk`, package listings, `checkupdates`) are not logged. |
| `runs/auto-<time>/tokens.jsonl` | Check results, repairs and control-flow events. |
| `runs/auto-<time>/report.json` | Result of the run: `run_id`, `started_at`, `finished_at`, `dry_run`, `out`, `exit_code`, `signal` and `child_stopped` (set only when a signal ended the run, otherwise `null`), `state`, `looks`, `crash`, `trouble`, `verdicts`, `updated`, `held`, `repairs`, `pacnew`, `reboot_required`, `wrote` (the run directory), and a `cve` block with the `arch-audit` findings (they never change the exit status). |
| `runs/kernel-<time>/run.log` | Kernel selection. |
| `last-auto.json` | `report.json` of the last update that was not a dry run, an interrupted one included. Source of `PENDING` and of the `l` key. A run that stopped at a check has this report but no `run.log`. |
| `last-patrol.json`, `patrol-history.json` | The last patrol, and a summary of the last 12. |
| `census.json` | Store failures first seen on this machine. |
| `keep/` | Pre-update records `auto-<time>/`, unless `keep` is set. |
| `auto.lock` | Lock file for the update. It is created by every run, dry runs included; the lock is the `flock` on it, not the file's existence. |
| `ask-window.log` | Output of the ask-mode terminal, and the reasons it could not run. |
| `ask-result.json` | The answer given at the last question of the weekly window (`--record`). Nothing else writes it. |
| `reports/` | Output of `scan-sessions` by default. |
| `~/.config/systemd/user/arch-update-auto.{service,timer}`, `arch-update-patrol.{service,timer}` | The user units. |
| `/etc/sudoers.d/zzz-arch-update-auto` | The sudoers rules, once you install them. |
| `~/.local/bin/arch-update`, `~/.local/share/arch-update-deck/`, `~/.local/share/applications/arch-update.desktop` | The installation. The desktop entry starts `arch-update` in a terminal, so it opens Update Day. |
| `/etc/default/grub.arch-updater-<time>.bak` | Backup made by each kernel selection. |
| `~/.cache/yay/<name>.quarantine-<run id>` | Cache directories set aside by `quarantine-aur-cache`. |

Nothing removes these on its own: `runs/` (every update and every
`auto --dry-run`; opening Update Day writes none), `ask-window.log`, the `grub.arch-updater-*.bak`
files and the `*.quarantine-*` directories grow until you delete them. Only
`keep/` is pruned, to the last four records. Delete a quarantine directory or a
GRUB backup only after checking that you do not need it.

## Environment variables

| Variable | Effect |
|---|---|
| `ARCH_UPDATER_STATE` | State directory. Default `~/.local/state/arch-updater`. |
| `ARCH_UPDATER_HOME` | Directory that contains the `arch_updater` package; read by the launcher. Default `../share/arch-update-deck` next to it. |
| `ARCH_UPDATER_PREFIX` | Installation prefix; read by `install.sh` only. Default `~/.local`. |
| `XDG_CONFIG_HOME` | Where `arch-updater/auto.json` is looked for. Default `~/.config`. |
| `XDG_CACHE_HOME` | Where the `yay` cache is looked for: by the AUR sudoers rules and by `quarantine-aur-cache`. Default `~/.cache`. |
| `TERMINAL` | Terminal for ask mode and click-to-fix. See [Schedule and ask mode](#schedule-and-ask-mode). |
| `NO_COLOR` | Turns colour off on Update Day. |
| `TERM` | `dumb` turns off colour, cursor moves and redraws on Update Day. |
| `ARCH_UPDATE_HOLD` | Set by the `.desktop` entry. Update Day then waits for Enter before it closes when a visit ends without an update. |
| `DISPLAY`, `WAYLAND_DISPLAY` | Read by the timer's ask run: if neither is set, it does not open a window (status 20). |
| `PYTHONPATH` | The launcher puts the installation directory in front of it. |

## Dry run output

`arch-update auto --dry-run` prints the JSON report on stdout. On stderr it adds
one line saying where the report was written (`wrote` in the report; a dry run
keeps its `report.json` and `tokens.jsonl` there, and `auto.lock` in the state
directory), and, if the root check failed with `NO-ROOT`, a line saying that
this is expected until the sudoers rule is installed. A dry run takes no lock
and so cannot make the timer skip its week. It attempts no repair, so the
`repairs` list shows `"attempted": false`.

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

This needs `pytest`. The tests replace every command, the name lookup, the AUR
request and the sleeps with scripted ones (`tests/test_auto.py`,
`tests/test_day.py`); the frames of Update Day are compared with the files in
`tests/fixtures/`. This repository does not record which stores or repairs have
been run on a real machine. The `snap` store appears in the scripted tests only.
No test runs the `paru` command line.

To watch a repair on a live system, see "Verifying a repair" in
[START-HERE.md](../START-HERE.md#verifying-a-repair).
