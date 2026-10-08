from types import SimpleNamespace

from arch_updater import cve


def test_cve_scan_parses_arch_audit_json(monkeypatch) -> None:
    monkeypatch.setattr(cve.shutil, "which", lambda _: "/usr/bin/arch-audit")
    monkeypatch.setattr(cve, "run_capture", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, output='[{"name":"AVG-1"}]'))
    result = cve.scan_cves()
    assert result["available"] is True
    assert result["count"] == 1


def test_cve_scan_reports_missing_binary(monkeypatch) -> None:
    monkeypatch.setattr(cve.shutil, "which", lambda _: None)
    result = cve.scan_cves()
    assert result["available"] is False
    assert result["findings"] == []
