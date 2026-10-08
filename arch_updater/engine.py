from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence


DATA_DIR = Path(__file__).resolve().parent / "data"  # shipped inside the package, so a wheel carries it
CONFIG_PATH = DATA_DIR / "profiles.json"
VAULT_PATH = DATA_DIR / "failure-modes.json"
STATE_ROOT = Path(os.environ.get("ARCH_UPDATER_STATE", Path.home() / ".local/state/arch-updater"))
GRUB_DEFAULT_PATH = Path("/etc/default/grub")
GRUB_CFG_PATH = Path("/boot/grub/grub.cfg")
SOFT_FREE_BYTES = 6 * 1024**3
TRANSIENT_UNIT_PREFIXES = ("polkit-agent-helper@", "systemd-coredump@")
BLOCKING_MODES = {
    "HYPRLAND-ABI-FORK",
    "NEW-FAILED-UNIT",
    "KERNEL-BOOT-ARTIFACT-MISSING",
    "STALE-BOOTLOADER-CONFIG",
    "DKMS-PARTIAL-MODULE-FAILURE",
}


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    output: str


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def run_capture(argv: Sequence[str], timeout: int = 30) -> CommandResult:
    try:
        completed = subprocess.run(
            list(argv),
            text=True,
            errors="replace",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return CommandResult(list(argv), completed.returncode, completed.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CommandResult(list(argv), 127, str(exc))


def _append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(text)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        return


def stream_command(argv: Sequence[str], log_path: Path, dry_run: bool = False) -> CommandResult:
    printable = " ".join(argv)
    if dry_run:
        return CommandResult(list(argv), 0, f"DRY-RUN {printable}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    use_tty = sys.stdin.isatty() and sys.stdout.isatty()
    script_bin = shutil.which("script")
    _append_log(log_path, f"\n$ {printable}\n")
    try:
        if use_tty and script_bin:
            process = subprocess.Popen(
                [script_bin, "-q", "-e", "-f", "-a", "-c", shlex.join(list(argv)), str(log_path)],
                start_new_session=True,
            )
            try:
                returncode = process.wait()
            except KeyboardInterrupt:
                _kill_group(process.pid)
                try:
                    returncode = process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                returncode = 130
            output = log_path.read_text(encoding="utf-8", errors="replace")
            _append_log(log_path, f"\n[exit {returncode}]\n")
            return CommandResult(list(argv), returncode, output)

        process = subprocess.Popen(
            list(argv),
            text=True,
            stdin=sys.stdin if use_tty else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            start_new_session=True,
        )
        chunks: list[str] = []
        assert process.stdout is not None
        try:
            for line in process.stdout:
                print(line, end="")
                _append_log(log_path, line)
                chunks.append(line)
            returncode = process.wait()
        except KeyboardInterrupt:
            _kill_group(process.pid)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            returncode = 130
            chunks.append("\ninterrupted\n")
        _append_log(log_path, f"\n[exit {returncode}]\n")
        return CommandResult(list(argv), returncode, "".join(chunks))
    except FileNotFoundError as exc:
        message = f"executable not found: {argv[0] if argv else '?'}: {exc}"
        _append_log(log_path, message + "\n")
        return CommandResult(list(argv), 127, message)


def privilege_command() -> str:
    """Prefer sudo: it authenticates via PAM+tty directly, no D-Bus/logind session
    cookie involved. pkexec needs systemd-logind to map the caller's cgroup back to
    an active session; where the terminal does not run inside its login session's
    scope (some setups move terminals into custom slices) that mapping breaks and pkexec
    fails authentication ("No session for cookie") even with the correct password,
    with no upside since it still falls through to a plain text prompt when no
    graphical polkit agent is running. pkexec remains only as a last-resort fallback
    for machines that don't have sudo at all."""
    return shutil.which("sudo") or shutil.which("pkexec") or "sudo"


def _os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"')
    return result


def _package_version(package: str) -> str | None:
    result = run_capture(["pacman", "-Q", package])
    if result.returncode != 0:
        return None
    fields = result.output.strip().split()
    return fields[1] if len(fields) >= 2 else None


def _failed_units(user: bool = False) -> list[str]:
    argv = ["systemctl"]
    if user:
        argv.append("--user")
    argv.extend(["--failed", "--no-legend", "--plain"])
    result = run_capture(argv)
    units: list[str] = []
    for line in result.output.splitlines():
        fields = line.lstrip("● ").split()
        if fields and fields[0].endswith((".service", ".mount", ".socket", ".target")):
            units.append(fields[0])
    return sorted(set(units))


def is_transient_helper_unit(name: str) -> bool:
    return name.startswith(TRANSIENT_UNIT_PREFIXES)


AUR_HELPERS = ("yay", "paru")  # the first one installed reads the AUR queue, here and on Update Day alike


def read_aur_queue(has: Callable[[str], bool], capture: Callable[..., CommandResult]) -> tuple[str | None, CommandResult]:
    """The AUR queue as the first installed helper reports it: (helper, result); (None, 127) when there is none."""
    helper = next((tool for tool in AUR_HELPERS if has(tool)), None)
    if helper is None:
        return None, CommandResult([], 127, "")
    return helper, capture([helper, "-Qua"], timeout=180)


def _pending_updates() -> dict[str, Any]:
    repo = run_capture(["checkupdates"], timeout=120) if shutil.which("checkupdates") else CommandResult([], 127, "")
    helper, aur = read_aur_queue(lambda tool: shutil.which(tool) is not None, run_capture)
    repo_ok, aur_ok = repo.returncode in {0, 2}, aur.returncode in {0, 1}
    repo_lines = [line for line in repo.output.splitlines() if line.strip()] if repo_ok else []
    aur_lines = [line for line in aur.output.splitlines() if line.strip()] if aur_ok else []
    notes = {}
    if not repo_ok:  # a queue that could not be read is unknown, never "0"
        notes["repo"] = "cannot check: install pacman-contrib" if repo.returncode == 127 else f"cannot check: checkupdates exited {repo.returncode}"
    if not aur_ok:
        notes["aur"] = "no AUR helper (yay or paru) installed" if helper is None else f"cannot check: {helper} -Qua exited {aur.returncode}"
    return {
        "repo": len(repo_lines) if repo_ok else None,
        "aur": len(aur_lines) if aur_ok else None,
        "repo_packages": repo_lines,
        "aur_packages": aur_lines,
        "probe_rc": {"repo": repo.returncode, "aur": aur.returncode},
        "notes": notes,
    }


def detect_kernel() -> dict[str, Any]:
    running = os.uname().release
    cmdline = Path("/proc/cmdline").read_text(encoding="utf-8", errors="replace") if Path("/proc/cmdline").exists() else ""
    image_match = re.search(r"(?:^|\s)BOOT_IMAGE=(?:/boot)?/vmlinuz-([^\s]+)", cmdline)
    running_package = image_match.group(1) if image_match else None
    installed: list[dict[str, Any]] = []
    module_releases: dict[str, list[str]] = {}
    modules_root = Path("/usr/lib/modules")
    if modules_root.is_dir():
        for module_dir in sorted(modules_root.glob("*")):
            pkgbase = module_dir / "pkgbase"
            if not pkgbase.is_file():
                continue
            package_name = pkgbase.read_text(encoding="utf-8", errors="replace").strip()
            module_releases.setdefault(package_name, []).append(module_dir.name)
    preset_root = Path("/etc/mkinitcpio.d")
    for preset in sorted(preset_root.glob("*.preset")) if preset_root.exists() else []:
        package = preset.stem
        vmlinuz = Path("/boot") / f"vmlinuz-{package}"
        initramfs = Path("/boot") / f"initramfs-{package}.img"
        installed.append(
            {
                "package": package,
                "version": _package_version(package),
                "headers": _package_version(f"{package}-headers"),
                "module_releases": module_releases.get(package, []),
                "vmlinuz": str(vmlinuz),
                "vmlinuz_exists": vmlinuz.is_file() and vmlinuz.stat().st_size > 0,
                "initramfs": str(initramfs),
                "initramfs_exists": initramfs.is_file() and initramfs.stat().st_size > 0,
            }
        )
    grub_default = None
    grub_path = GRUB_DEFAULT_PATH
    if grub_path.exists():
        try:
            match = re.search(r'^GRUB_DEFAULT=(?:"([^"]*)"|([^\n#]*))', grub_path.read_text(encoding="utf-8"), re.MULTILINE)
        except (OSError, UnicodeDecodeError):  # unreadable: the default is unknown, which is what None says
            match = None
        if match:
            grub_default = (match.group(1) or match.group(2) or "").strip()
    return {
        "running_release": running,
        "running_package": running_package,
        "running_modules_present": Path("/usr/lib/modules", running).is_dir(),
        "installed": installed,
        "bootloader": "grub" if Path("/boot/grub/grub.cfg").exists() else "systemd-boot" if Path("/boot/loader").exists() else "unknown",
        "configured_default": grub_default,
    }


def default_kernel_package(kernel: dict[str, Any]) -> str | None:
    configured = kernel.get("configured_default") or ""
    match = re.search(r"with Linux ([^\s\"']+)", configured)
    if match:
        return match.group(1)
    return kernel.get("running_package")


def grub_default_entry(package: str, grub_cfg: str) -> str | None:
    submenu: str | None = None
    pattern = re.compile(r"menuentry\s+'([^']*with Linux " + re.escape(package) + r")'")
    for line in grub_cfg.splitlines():
        sub = re.search(r"submenu\s+'([^']+)'", line)
        if sub:
            submenu = sub.group(1)
            continue
        entry = pattern.search(line)
        if entry:
            title = entry.group(1)
            return f"{submenu}>{title}" if submenu else title
    return None


def available_kernel_profiles() -> list[dict[str, Any]]:
    profiles = load_json(CONFIG_PATH)["kernels"]
    detected = detect_kernel()
    installed_names = {item["package"] for item in detected["installed"]}
    result: list[dict[str, Any]] = []
    for profile in profiles:
        item = dict(profile)
        package = item.get("package")
        item["installed"] = package in installed_names if package else True
        if package is None or package in installed_names:
            item["available"] = True
        else:
            item["available"] = run_capture(["pacman", "-Si", package]).returncode == 0
        item["detected"] = package == detected["running_package"] if package else False
        if item["available"]:
            result.append(item)
    known = {item.get("package") for item in result}
    for installed in detected["installed"]:
        package = installed["package"]
        if package in known:
            continue
        result.append(
            {
                "id": package,
                "label": f"Installed: {package}",
                "package": package,
                "headers": f"{package}-headers",
                "installed": True,
                "available": True,
                "detected": package == detected["running_package"],
            }
        )
    return result


def _pacnew_files() -> list[str]:
    if shutil.which("pacdiff"):
        result = run_capture(["pacdiff", "-o"])
        return sorted(line for line in result.output.splitlines() if line.endswith((".pacnew", ".pacsave")))
    return []


def _hyprland_health() -> dict[str, Any]:
    binary = shutil.which("Hyprland")
    if not binary:
        return {"installed": False, "missing_libraries": []}
    version = run_capture([binary, "--version"]).output.splitlines()
    libraries = run_capture(["ldd", binary]).output.splitlines()
    missing = [line.strip() for line in libraries if "not found" in line]
    mixed = run_capture(["pacman", "-Qm"]).output.splitlines()
    mixed = [line for line in mixed if re.match(r"^(?:hypr|aquamarine)", line)]
    return {
        "installed": True,
        "version": version[0] if version else "unknown",
        "missing_libraries": missing,
        "foreign_chain": mixed,
    }


def snapshot(include_updates: bool = True, include_slow: bool | None = None) -> dict[str, Any]:
    if include_slow is None:
        include_slow = include_updates
    usage = shutil.disk_usage("/")
    data: dict[str, Any] = {
        "timestamp": datetime.now().astimezone().isoformat(),
        "os": _os_release(),
        "root": {"free_bytes": usage.free, "total_bytes": usage.total},
        "pacman_lock": Path("/var/lib/pacman/db.lck").exists(),
        "kernel": detect_kernel(),
        "failed_units": {"system": _failed_units(), "user": _failed_units(user=True)},
        "pacnew": _pacnew_files(),
        "hyprland": _hyprland_health() if include_slow else {"installed": bool(shutil.which("Hyprland")), "missing_libraries": []},
        "dkms": run_capture(["dkms", "status"]).output.splitlines() if include_slow and shutil.which("dkms") else [],
        "gpu": None,
    }
    if include_updates:
        data["updates"] = _pending_updates()
    else:
        data["updates"] = {"repo": None, "aur": None, "repo_packages": [], "aur_packages": [], "probe_rc": {}}
    if include_slow and shutil.which("nvidia-smi"):
        gpu = run_capture(
            [
                "nvidia-smi",
                "--query-gpu=name,clocks.gr,clocks.max.gr,power.draw,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
        )
        data["gpu"] = gpu.output.strip() if gpu.returncode == 0 else None
    return data


def classify_failures(text: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for mode in load_json(VAULT_PATH)["modes"]:
        if mode.get("classify_output") is False:
            continue
        if re.search(mode["match"], text):
            matches.append(mode)
    return matches


def attest(before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> dict[str, Any]:
    after = after if after is not None else snapshot(include_updates=True, include_slow=True)
    issues: list[dict[str, Any]] = []
    kernel = after["kernel"]
    if not kernel["running_modules_present"]:
        issues.append(
            {
                "mode": "RUNNING-KERNEL-MODULES-MISSING",
                "message": "running kernel module directory is missing",
                "reboot_required": True,
            }
        )
    missing_hypr = after.get("hyprland", {}).get("missing_libraries") or []
    if missing_hypr:
        issues.append(
            {
                "mode": "HYPRLAND-ABI-FORK",
                "message": "; ".join(missing_hypr),
                "reboot_required": False,
            }
        )
    grub_text = after.get("grub_cfg_text")
    if grub_text is None:
        grub_cfg = Path("/boot/grub/grub.cfg")
        grub_text = grub_cfg.read_text(encoding="utf-8", errors="replace") if grub_cfg.exists() else ""
    needs_nvidia_dkms = _package_version("nvidia-open-dkms") is not None or _package_version("nvidia-dkms") is not None
    default_package = default_kernel_package(kernel)
    relevant_dkms = {kernel.get("running_package"), default_package} - {None}
    for installed_kernel in kernel["installed"]:
        package = installed_kernel["package"]
        if not installed_kernel["vmlinuz_exists"] or not installed_kernel["initramfs_exists"]:
            issues.append(
                {
                    "mode": "KERNEL-BOOT-ARTIFACT-MISSING",
                    "message": f"kernel boot artifact missing: {package}",
                    "reboot_required": False,
                }
            )
        if (
            kernel["bootloader"] == "grub"
            and package == default_package
            and f"/vmlinuz-{package}" not in grub_text
        ):
            issues.append(
                {
                    "mode": "STALE-BOOTLOADER-CONFIG",
                    "message": f"bootloader does not reference selected kernel: {package}",
                    "reboot_required": False,
                }
            )
        if needs_nvidia_dkms and package in relevant_dkms:
            for release in installed_kernel["module_releases"]:
                nvidia_ready = any(
                    line.startswith("nvidia/") and release in line and "installed" in line
                    for line in after.get("dkms") or []
                )
                if not nvidia_ready:
                    issues.append(
                        {
                            "mode": "DKMS-PARTIAL-MODULE-FAILURE",
                            "message": f"NVIDIA DKMS missing for {release}",
                            "reboot_required": False,
                        }
                    )
    inventory: list[dict[str, Any]] = []
    for path in after.get("pacnew") or []:
        inventory.append({"mode": "PACNEW-PENDING", "message": path, "reboot_required": False})
    if before:
        for scope in ("system", "user"):
            previous = set(before.get("failed_units", {}).get(scope, []))
            current = set(after.get("failed_units", {}).get(scope, []))
            for unit in sorted(current - previous):
                if is_transient_helper_unit(unit):
                    continue
                issues.append(
                    {
                        "mode": "NEW-FAILED-UNIT",
                        "message": f"new failed unit ({scope}): {unit}",
                        "reboot_required": False,
                    }
                )
    pending = after.get("updates") or {"repo": 0, "aur": 0}
    healthy = not any(issue["mode"] in BLOCKING_MODES for issue in issues)
    return {
        "healthy": healthy,
        "reboot_required": any(issue["reboot_required"] for issue in issues),
        "issues": issues,
        "inventory": inventory,
        "pending": {"repo": pending.get("repo"), "aur": pending.get("aur")},
        "snapshot": after,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def aur_cache_root() -> Path:
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "yay"


def quarantine_missing_aur_caches(output: str, run_id: str) -> list[dict[str, str]]:
    """Move only a failed yay cache which cannot contain a PKGBUILD.

    The package name and path both come from yay's raw error output.  A cache
    is never deleted, symlinks are rejected, and the caller gets at most one
    retry after this recovery.
    """
    root = aur_cache_root()
    root_resolved = root.resolve()
    candidates = re.findall(r"(?im)^\s*->\s*error downloading sources:\s*([^\r\n]+?)\s*$", output)
    recovered: list[dict[str, str]] = []
    for raw_path in candidates:
        candidate = Path(raw_path.strip())
        if (
            candidate.parent.resolve() != root_resolved
            or candidate.is_symlink()
            or not candidate.is_dir()
            or (candidate / "PKGBUILD").is_file()
        ):
            continue
        quarantine = root / f"{candidate.name}.quarantine-{run_id}"
        suffix = 1
        while quarantine.exists():
            quarantine = root / f"{candidate.name}.quarantine-{run_id}-{suffix}"
            suffix += 1
        candidate.replace(quarantine)
        recovered.append(
            {
                "package": candidate.name,
                "from": str(candidate),
                "to": str(quarantine),
                "reason": "missing PKGBUILD",
            }
        )
    return recovered


def _roll_back(privileged: str, backup: str, grub_path: Path, log_path: Path) -> str:
    """Put the GRUB defaults back from the backup and regenerate the menu; say truthfully how far that got."""
    restored = stream_command([privileged, "cp", "--", backup, str(grub_path)], log_path)
    if restored.returncode:
        return f"the rollback FAILED too (cp exit {restored.returncode}): {grub_path} may still be changed; the backup is {backup}"
    regenerated = stream_command([privileged, "grub-mkconfig", "-o", str(GRUB_CFG_PATH)], log_path)
    if regenerated.returncode:
        return f"{grub_path} was restored from {backup}, but regenerating {GRUB_CFG_PATH} failed (exit {regenerated.returncode}); run: sudo grub-mkconfig -o {GRUB_CFG_PATH}"
    return "configuration was rolled back"


def select_kernel(profile_id: str, dry_run: bool = False) -> dict[str, Any]:
    profiles = available_kernel_profiles()
    profile = next((item for item in profiles if item["id"] == profile_id), None)
    if not profile:
        raise ValueError(f"kernel profile is unavailable: {profile_id}")
    if profile["id"] == "auto":
        return {"changed": False, "kernel": detect_kernel(), "reason": "kept detected kernel"}
    package = profile["package"]
    headers = profile.get("headers")
    privileged = privilege_command()
    run_id = datetime.now().astimezone().strftime("kernel-%Y%m%dT%H%M%S%z")
    log_path = STATE_ROOT / "runs" / run_id / "run.log"
    install = [privileged, "pacman", "-S", "--needed", package]
    if headers:
        install.append(headers)
    install_result = stream_command(install, log_path, dry_run=dry_run)
    if install_result.returncode != 0:
        raise RuntimeError(f"kernel package installation failed: {install_result.returncode}")

    bootloader = detect_kernel()["bootloader"]
    if bootloader != "grub":
        return {
            "changed": True,
            "package": package,
            "bootloader_changed": False,
            "reason": f"kernel installed; {bootloader} default selection is not supported",
        }

    grub_path = GRUB_DEFAULT_PATH
    grub_cfg_path = GRUB_CFG_PATH
    try:
        original = grub_path.read_text(encoding="utf-8")
        grub_cfg = grub_cfg_path.read_text(encoding="utf-8", errors="replace") if grub_cfg_path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"kernel {package} is installed, but GRUB could not be read ({exc}); GRUB was left untouched") from exc
    entry = grub_default_entry(package, grub_cfg) or f"Advanced options for Arch Linux>Arch Linux, with Linux {package}"
    replacement = f'GRUB_DEFAULT="{entry}"'
    if re.search(r"^GRUB_DEFAULT=", original, re.MULTILINE):
        updated = re.sub(r"^GRUB_DEFAULT=.*$", replacement, original, count=1, flags=re.MULTILINE)
    else:
        updated = f"{replacement}\n{original}"
    backup = f"{grub_path}.arch-updater-{run_id}.bak"
    if dry_run:
        return {
            "changed": True,
            "package": package,
            "bootloader_changed": True,
            "dry_run": True,
            "planned_default": entry,
        }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(updated)
        temporary = handle.name
    try:
        backup_result = stream_command([privileged, "cp", "--", str(grub_path), backup], log_path)
        if backup_result.returncode:
            # Without a backup there is nothing to roll back to, so nothing may be changed.
            raise RuntimeError(
                f"could not back up {grub_path} (exit {backup_result.returncode}); GRUB was left untouched"
            )
        install_grub = stream_command([privileged, "install", "-m", "0644", "--", temporary, str(grub_path)], log_path)
        regenerate = stream_command([privileged, "grub-mkconfig", "-o", str(GRUB_CFG_PATH)], log_path)
        if install_grub.returncode or regenerate.returncode:
            raise RuntimeError(f"GRUB update failed; {_roll_back(privileged, backup, grub_path, log_path)}")
    finally:
        Path(temporary).unlink(missing_ok=True)
    try:
        grub_cfg = GRUB_CFG_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"GRUB was rewritten, but {GRUB_CFG_PATH} could not be read to check it ({exc}); the backup is {backup}") from exc
    if f"/vmlinuz-{package}" not in grub_cfg:
        raise RuntimeError(f"bootloader does not reference selected kernel; {_roll_back(privileged, backup, grub_path, log_path)}")
    return {
        "changed": True,
        "package": package,
        "headers": headers,
        "bootloader_changed": True,
        "backup": backup,
        "planned_default": entry,
        "reboot_required": detect_kernel()["running_package"] != package,
    }
