from pathlib import Path

import pytest
from test_auto import box  # noqa: F401 - a fixture

from arch_updater.engine import (
    CommandResult,
    attest,
    classify_failures,
    grub_default_entry,
    privilege_command,
    quarantine_missing_aur_caches,
    run_update,
)
from arch_updater.transcripts import _codex_command, _execution_session_ids, classify_command, redact


def test_classifies_real_update_commands() -> None:
    assert classify_command("sudo pacman -Syu --noconfirm") == "repo-update"
    assert classify_command("yay -Sua --sudo /usr/bin/pkexec") == "aur-update"
    assert classify_command("pkexec pacman -S --needed linux-lts linux-lts-headers") == "kernel-change"
    assert classify_command("bash -lc 'sudo pacman -U /tmp/example.pkg.tar.zst'") == "local-package-update"


def test_rejects_queries_and_mentions() -> None:
    assert classify_command("pacman -Q linux") is None
    assert classify_command("pacman -Si linux-lts") is None
    assert classify_command("rg -n 'pacman -Syu' ~/.codex/sessions") is None
    assert classify_command("printf 'sudo pacman -Syu\\n'") is None


def test_redacts_secrets() -> None:
    value = redact("API_TOKEN=abc curl -H 'Authorization: Bearer very-secret' https://u:p@example.test")
    assert "abc" not in value
    assert "very-secret" not in value
    assert "u:p" not in value


def test_failure_vault_matches_live_modes() -> None:
    modes = {item["id"] for item in classify_failures("sudo: a password is required")}
    assert "SUDO-TIMESTAMP-EXPIRED" in modes
    modes = {item["id"] for item in classify_failures("Unknown download protocol: git-lfs")}
    assert "AUR-GIT-LFS-PROTOCOL" in modes
    modes = {
        item["id"]
        for item in classify_failures(
            " -> error downloading sources: /home/test/.cache/yay/example-app-git \n"
            " -> failed to parse example-app-git: Unable to read file: .SRCINFO: no such file"
        )
    }
    assert "AUR-CACHE-PKGBUILD-MISSING" in modes


def test_quarantines_only_direct_missing_aur_cache(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    package = cache_root / "yay" / "example-app-git"
    package.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    output = f" -> error downloading sources: {package} \n"

    recovered = quarantine_missing_aur_caches(output, "test-run")

    assert [item["package"] for item in recovered] == ["example-app-git"]
    assert not package.exists()
    assert (cache_root / "yay" / "example-app-git.quarantine-test-run").is_dir()


def test_does_not_quarantine_valid_or_indirect_aur_paths(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    valid = cache_root / "yay" / "valid"
    valid.mkdir(parents=True)
    (valid / "PKGBUILD").write_text("pkgname=valid\n", encoding="utf-8")
    indirect = cache_root / "yay" / "nested" / "broken"
    indirect.mkdir(parents=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    output = f" -> error downloading sources: {valid}\n -> error downloading sources: {indirect}\n"

    assert quarantine_missing_aur_caches(output, "test-run") == []
    assert valid.is_dir()
    assert indirect.is_dir()


def test_aur_update_recovers_missing_cache_once_and_keeps_initial_failure(tmp_path: Path, monkeypatch) -> None:
    cache_root = tmp_path / "cache"
    package = cache_root / "yay" / "example-app-git"
    package.mkdir(parents=True)
    state_root = tmp_path / "state"
    initial_output = f" -> error downloading sources: {package} \n"
    calls: list[list[str]] = []
    responses = iter(
        [
            CommandResult(["yay", "-Sua"], 1, initial_output),
            CommandResult(["yay", "-Sua"], 0, "installed"),
        ]
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.setattr("arch_updater.engine.STATE_ROOT", state_root)
    monkeypatch.setattr("arch_updater.engine._preflight", lambda *_args, **_kwargs: {"failed_units": {}})
    monkeypatch.setattr("arch_updater.engine.attest", lambda *_args, **_kwargs: {"healthy": True, "snapshot": {}})
    monkeypatch.setattr("arch_updater.engine.shutil.which", lambda name: "/usr/bin/yay" if name == "yay" else None)

    def fake_stream(argv, _log_path, dry_run=False):
        calls.append(argv)
        return next(responses)

    monkeypatch.setattr("arch_updater.engine.stream_command", fake_stream)

    report = run_update("aur")

    assert report["failed"] is False
    assert len(calls) == 2
    assert report["commands"][0]["attempt"] == "initial"
    assert report["commands"][0]["returncode"] == 1
    assert report["commands"][1]["attempt"] == "recovery-retry"
    assert report["commands"][1]["recovered_caches"][0]["package"] == "example-app-git"
    assert (cache_root / "yay" / f"example-app-git.quarantine-{report['run_id']}").is_dir()


def test_project_assets_exist() -> None:
    from arch_updater.engine import CONFIG_PATH, VAULT_PATH

    package = Path(__file__).resolve().parents[1] / "arch_updater"
    assert VAULT_PATH == package / "data/failure-modes.json" and VAULT_PATH.is_file()
    assert CONFIG_PATH == package / "data/profiles.json" and CONFIG_PATH.is_file()


def test_codex_js_object_and_async_session_are_structural() -> None:
    payload = {
        "type": "custom_tool_call",
        "name": "exec",
        "input": 'const r = await tools.exec_command({cmd:"yay -Sua",yield_time_ms:1000});',
    }
    assert _codex_command(payload) == "yay -Sua"
    assert _execution_session_ids('{"chunk_id":"abc","session_id":78423,"output":"ok"}') == {"78423"}
    assert _execution_session_ids('1250:{"chunk_id":"abc","session_id":78423,"output":"grep hit"}') == set()


def test_privilege_prefers_sudo(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.engine.shutil.which", lambda name: f"/usr/bin/{name}")
    assert privilege_command() == "/usr/bin/sudo"


def test_skipping_build_is_not_split_package_failure() -> None:
    modes = {item["id"] for item in classify_failures("already made -- skipping build")}
    assert "AUR-SPLIT-PACKAGE-GRANULARITY" not in modes


def test_pacnew_path_is_not_a_command_failure() -> None:
    modes = {item["id"] for item in classify_failures("/etc/locale.gen.pacnew")}
    assert "PACNEW-PENDING" not in modes


def test_grub_default_entry_parses_live_titles() -> None:
    grub_cfg = (Path(__file__).resolve().parent / "grub.cfg.fixture").read_text(encoding="utf-8")
    assert (
        grub_default_entry("linux-cachyos-bore", grub_cfg)
        == "Advanced options for Arch Linux>Arch Linux, with Linux linux-cachyos-bore"
    )
    assert (
        grub_default_entry("linux-zen-slim", grub_cfg)
        == "Advanced options for Arch Linux>Arch Linux, with Linux linux-zen-slim"
    )
    assert grub_default_entry("linux-lts", grub_cfg) is None


def _snapshot(**overrides):
    data = {
        "kernel": {
            "running_release": "7.1.8-1-cachyos-bore",
            "running_package": "linux-cachyos-bore",
            "running_modules_present": True,
            "installed": [
                {
                    "package": "linux-cachyos-bore",
                    "vmlinuz_exists": True,
                    "initramfs_exists": True,
                    "module_releases": ["7.1.8-1-cachyos-bore"],
                },
                {
                    "package": "linux-zen-slim",
                    "vmlinuz_exists": True,
                    "initramfs_exists": True,
                    "module_releases": ["6.16-zen-slim"],
                },
            ],
            "bootloader": "grub",
            "configured_default": "Advanced options for Arch Linux>Arch Linux, with Linux linux-cachyos-bore",
        },
        "hyprland": {"missing_libraries": []},
        "dkms": [],
        "pacnew": ["/etc/locale.gen.pacnew"],
        "failed_units": {"system": [], "user": []},
        "updates": {"repo": 12, "aur": 3},
        "grub_cfg_text": "linux /vmlinuz-linux-cachyos-bore\n",
    }
    data.update(overrides)
    return data


def test_attest_healthy_with_pending(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.engine._package_version", lambda _pkg: None)
    result = attest(after=_snapshot())
    assert result["healthy"] is True
    assert result["pending"] == {"repo": 12, "aur": 3}
    assert result["inventory"][0]["mode"] == "PACNEW-PENDING"
    assert all(issue["mode"] != "PACNEW-PENDING" for issue in result["issues"])
    assert all(issue["mode"] != "STALE-BOOTLOADER-CONFIG" for issue in result["issues"])


def test_attest_ignores_polkit_helper_delta(monkeypatch) -> None:
    monkeypatch.setattr("arch_updater.engine._package_version", lambda _pkg: None)
    before = {"failed_units": {"system": [], "user": []}}
    after = _snapshot(
        failed_units={
            "system": ["polkit-agent-helper@1-49172-2931537_2923747-0.service", "sshd.service"],
            "user": [],
        }
    )
    result = attest(before=before, after=after)
    messages = [issue["message"] for issue in result["issues"] if issue["mode"] == "NEW-FAILED-UNIT"]
    assert any("sshd.service" in message for message in messages)
    assert all("polkit-agent-helper" not in message for message in messages)


def test_help_lists_every_command_and_prints_no_suppress_marker(capsys) -> None:
    import pytest

    from arch_updater.main import main

    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    assert "==SUPPRESS==" not in text
    assert "patrol" in text


def test_a_queue_that_cannot_be_read_is_unknown_not_zero(monkeypatch) -> None:
    from arch_updater import engine
    from arch_updater.ui import render_status_page

    monkeypatch.setattr(engine.shutil, "which", lambda tool: None)
    pending = engine._pending_updates()
    assert pending["repo"] is None and pending["notes"]["repo"] == "cannot check: install pacman-contrib"
    state = _snapshot(updates=pending, root={"free_bytes": 10**11}, pacman_lock=False)
    page = render_status_page(state)
    assert "repos ?" in page and "install pacman-contrib" in page


def test_installer_replaces_the_package_quotes_exec_and_warns_about_path(tmp_path) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]
    prefix = tmp_path / "pre fix"
    env = {"PATH": "/usr/bin:/bin", "ARCH_UPDATER_PREFIX": str(prefix), "HOME": str(tmp_path)}

    def install() -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(root / "install.sh")], env=env, capture_output=True, text=True, check=True)

    first = install()
    share = prefix / "share/arch-update-deck"
    (share / "arch_updater/removed_upstream.py").write_text("stale = True\n")
    (share / "vault").mkdir()
    second = install()
    assert not (share / "arch_updater/removed_upstream.py").exists() and not (share / "vault").exists()
    assert (share / "arch_updater/data/failure-modes.json").is_file() and (share / "arch_updater/control.tis").is_file()
    assert (share / "LICENSE").read_text() == (root / "LICENSE").read_text()  # the README links it
    assert f'Exec="{prefix}/bin/arch-update" menu' in (prefix / "share/applications/arch-update.desktop").read_text()
    assert "not in your PATH" in first.stderr and "not in your PATH" in second.stderr
    env["PATH"] += f":{prefix}/bin"
    assert "not in your PATH" not in install().stderr


def test_a_built_wheel_carries_the_data_files_and_one_version(tmp_path) -> None:
    import shutil
    import subprocess
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[1]
    if shutil.which("pip") is None and subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True).returncode:
        pytest.skip("pip is not available")
    source = tmp_path / "source"  # built from a copy, so no build/ or egg-info lands in the repository
    shutil.copytree(root, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "reports"))
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-w", str(tmp_path), str(source)],
        capture_output=True,
        text=True,
    )
    if built.returncode:
        pytest.skip(f"cannot build a wheel here: {built.stderr[-200:]}")
    (wheel,) = tmp_path.glob("arch_update_deck-*.whl")
    from arch_updater import __version__

    assert wheel.name.startswith(f"arch_update_deck-{__version__}-")
    names = zipfile.ZipFile(wheel).namelist()
    assert {"arch_updater/data/failure-modes.json", "arch_updater/data/profiles.json", "arch_updater/control.tis"} <= set(names)


def _preflight_machine(monkeypatch, tmp_path, free_gib: float, after_gib: float | None = None):
    from arch_updater import engine

    ran: list[list[str]] = []
    freed = {"now": free_gib}
    monkeypatch.setattr(engine, "snapshot", lambda **_kwargs: {"pacman_lock": False, "root": {"free_bytes": int(free_gib * 1024**3)}})
    monkeypatch.setattr(engine, "run_capture", lambda argv, **_kwargs: CommandResult(list(argv), 0, ""))
    monkeypatch.setattr(engine, "privilege_command", lambda: "sudo")

    def fake_stream(argv, log_path, dry_run=False):
        ran.append(list(argv))
        freed["now"] = free_gib if after_gib is None else after_gib
        return CommandResult(list(argv), 0, "")

    monkeypatch.setattr(engine, "stream_command", fake_stream)
    monkeypatch.setattr(engine.shutil, "disk_usage", lambda _path: type("U", (), {"free": int(freed["now"] * 1024**3)})())
    return engine, ran


def test_interactive_run_cleans_with_paccache_rk1_between_four_and_six_gib(monkeypatch, tmp_path) -> None:
    engine, ran = _preflight_machine(monkeypatch, tmp_path, 5.0, after_gib=7.0)
    engine._preflight("r", True, tmp_path / "run.log")
    assert ran == [["sudo", "paccache", "-rk1"]]


def test_the_hard_floor_of_four_gib_stops_a_run_even_after_cleaning(monkeypatch, tmp_path) -> None:
    import pytest

    engine, ran = _preflight_machine(monkeypatch, tmp_path, 5.0, after_gib=3.5)
    with pytest.raises(RuntimeError, match="hard floor"):
        engine._preflight("r", True, tmp_path / "run.log")
    assert ran == [["sudo", "paccache", "-rk1"]]


def test_the_unattended_look_refuses_below_six_gib(box) -> None:
    from arch_updater import watch

    box.sh.free = int(5.9 * 1024**3)
    verdict = watch.probe_disk(watch.Scene(box.sh, {}, None, None, None)).verdicts[0]
    assert (verdict.status, verdict.case) == ("fail", "ROOT-SPACE-LOW")
    box.sh.free = int(6.1 * 1024**3)
    assert watch.probe_disk(watch.Scene(box.sh, {}, None, None, None)).verdicts[0].status == "pass"


def test_a_failure_with_no_output_is_recorded_once_and_recognised_next_time(tmp_path) -> None:
    from arch_updater import census

    cases: list = []
    log = tmp_path / "run.log"
    first = census.record(cases, tmp_path, "", "pacman", log)
    assert census.recognise(cases, "") is first and census.recognise(cases, "  \n") is first
    again = census.record(cases, tmp_path, "\n", "pacman", log)
    assert again is first and len(cases) == 1 and first.seen == 2
    assert [case.id for case in census.load(tmp_path) if case.id.startswith("LOCAL-")] == [first.id]
    assert census.recognise(cases, "error: something else") is None


def test_readme_status_line_names_the_current_version() -> None:
    from arch_updater import __version__

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert f"Version {__version__}." in readme
