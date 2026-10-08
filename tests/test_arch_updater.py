from pathlib import Path

import pytest
from test_auto import box  # noqa: F401 - a fixture

from arch_updater.engine import (
    attest,
    classify_failures,
    grub_default_entry,
    privilege_command,
    quarantine_missing_aur_caches,
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


def test_help_lists_the_commands_in_order_one_plain_line_each_and_none_of_the_old_menu(capsys) -> None:
    import re

    import pytest

    from arch_updater.main import main

    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    assert "==SUPPRESS==" not in text
    listed = re.findall(r"^    (\S+)\s{2,}\S", text, re.MULTILINE)
    assert listed == ["status", "kernel", "attest", "auto", "schedule", "patrol", "share-report", "dotfiles", "brief", "learn", "census", "vault", "cve", "scan-sessions"]
    for gone in ("menu", "preview", "snapshot-json", "search", "plan", "run"):
        with pytest.raises(SystemExit):
            main([gone])
        capsys.readouterr()


def test_a_queue_that_cannot_be_read_is_unknown_not_zero(monkeypatch) -> None:
    from arch_updater import engine
    from arch_updater.main import render_status

    monkeypatch.setattr(engine.shutil, "which", lambda tool: None)
    pending = engine._pending_updates()
    assert pending["repo"] is None and pending["notes"]["repo"] == "cannot check: install pacman-contrib"
    state = _snapshot(updates=pending, root={"free_bytes": 10**11}, pacman_lock=False)
    page = render_status(state)
    assert "repos ?" in page and "install pacman-contrib" in page


def test_status_and_the_patrol_read_the_aur_queue_from_paru_when_it_is_the_only_helper(monkeypatch) -> None:
    from arch_updater import engine
    from arch_updater.engine import CommandResult

    asked = []
    present = {"checkupdates", "paru"}
    monkeypatch.setattr(engine.shutil, "which", lambda tool: f"/usr/bin/{tool}" if tool in present else None)
    monkeypatch.setattr(engine, "run_capture", lambda argv, timeout=30: asked.append(argv) or CommandResult(argv, 0, "a 1 -> 2\n" if argv[0] == "paru" else ""))
    pending = engine._pending_updates()
    assert ["paru", "-Qua"] in asked and ["yay", "-Qua"] not in asked
    assert pending["aur"] == 1 and pending["notes"] == {}
    present.clear()
    assert engine._pending_updates()["notes"]["aur"] == "no AUR helper (yay or paru) installed"


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
    assert f'Exec=env ARCH_UPDATE_HOLD=1 "{prefix}/bin/arch-update"\n' in (prefix / "share/applications/arch-update.desktop").read_text()
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

    # One version, as pip writes it (PEP 440 drops leading zeros: 0.20.01 is 0.20.1 to it).
    normalized = ".".join(str(int(part)) for part in __version__.split("."))
    assert wheel.name.startswith(f"arch_update_deck-{normalized}-")
    names = zipfile.ZipFile(wheel).namelist()
    assert {"arch_updater/data/failure-modes.json", "arch_updater/data/profiles.json", "arch_updater/control.tis"} <= set(names)


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
