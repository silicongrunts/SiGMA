from pathlib import Path

import pytest

from app.services import tex_service as tex_module
from app.services.tex_service import TeXService


def _make_tlmgr(root: Path, year: str) -> None:
    tlmgr = root / year / "bin" / "x86_64-linux" / "tlmgr"
    tlmgr.parent.mkdir(parents=True, exist_ok=True)
    tlmgr.write_text("#!/bin/sh\n", encoding="utf-8")


def test_tex_service_reports_current_link_and_installed_years(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    _make_tlmgr(tmp_path, "2026")
    (tmp_path / "current").symlink_to("2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))
    monkeypatch.setattr(tex_module, "DEFAULT_TEXLIVE_YEAR", "2025")

    service = TeXService()

    assert service.get_status()["current_year"] == "2025"
    assert service.get_status()["latest_installed_year"] == "2026"
    assert service.get_status()["installed_years"] == ["2025", "2026"]
    assert service._tlmgr_bin("2025") == str(tmp_path / "2025" / "bin" / "x86_64-linux" / "tlmgr")


def test_tex_service_uses_year_specific_repositories(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()
    status = service.get_status()

    assert "2025/tlnet-final" in status["repositories"]["official"]
    assert status["current_repositories"]["official"].endswith("/systems/texlive/tlnet")


def test_install_year_repository_uses_historic_archive_for_past_years():
    service = TeXService()
    past_year = str(tex_module.utcnow().year - 1)

    url = service._install_year_repository_url("official", year=past_year)

    assert f"{past_year}/tlnet-final" in url


@pytest.mark.asyncio
async def test_tex_service_rejects_invalid_switch_year(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()

    with pytest.raises(ValueError, match="Invalid TeX Live target year"):
        await service.switch_year(target_year="not-a-year").__anext__()


@pytest.mark.asyncio
async def test_switch_year_activates_installed_year_without_restart(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    _make_tlmgr(tmp_path, "2026")
    (tmp_path / "current").symlink_to("2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()

    events = [event async for event in service.switch_year(target_year="2026")]

    assert (tmp_path / "current").readlink() == Path("2026")
    assert service.get_status()["current_year"] == "2026"
    assert any("Switched TeX Live to 2026" in event for event in events)
    assert not any("restart_required" in event for event in events)


@pytest.mark.asyncio
async def test_switch_year_installs_base_packages_for_new_year(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    (tmp_path / "current").symlink_to("2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()
    commands = []

    async def fake_run_process(args, success_data=None, exit_codes=None):
        commands.append(args)
        if exit_codes is not None:
            exit_codes.append(0)
        yield service._event("done", {"returncode": 0, **(success_data or {})})

    monkeypatch.setattr(service, "_run_process", fake_run_process)

    events = [event async for event in service.switch_year(target_year="2026")]

    script = commands[0][2]
    assert "tlmgr install" in script
    assert "TEXMFHOME ~/texmf" not in script
    assert f"TEXMFHOME {Path.home()}/texmf" in script
    for package in tex_module.TEX_BASE_PACKAGES:
        assert package in script
    assert any("TeX Live 2026 installed and activated" in event for event in events)


@pytest.mark.asyncio
async def test_switch_year_removes_incomplete_installation_before_reinstall(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    incomplete_root = tmp_path / "2026"
    incomplete_root.mkdir()
    (incomplete_root / "stale.txt").write_text("stale", encoding="utf-8")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()

    async def fake_run_process(args, success_data=None, exit_codes=None):
        if exit_codes is not None:
            exit_codes.append(0)
        _make_tlmgr(tmp_path, "2026")
        yield service._event("done", {"returncode": 0, **(success_data or {})})

    monkeypatch.setattr(service, "_run_process", fake_run_process)

    events = [event async for event in service.switch_year(target_year="2026")]

    assert not (tmp_path / "2026" / "stale.txt").exists()
    assert (tmp_path / "2026" / "bin" / "x86_64-linux" / "tlmgr").exists()
    assert any("Removing incomplete TeX Live 2026" in event for event in events)


@pytest.mark.asyncio
async def test_switch_year_cleans_failed_incomplete_installation(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()

    async def fake_run_process(args, success_data=None, exit_codes=None):
        (tmp_path / "2026").mkdir()
        if exit_codes is not None:
            exit_codes.append(1)
        yield service._event("error", {"returncode": 1})

    monkeypatch.setattr(service, "_run_process", fake_run_process)

    events = [event async for event in service.switch_year(target_year="2026")]

    assert not (tmp_path / "2026").exists()
    assert any('"returncode": 1' in event for event in events)


@pytest.mark.asyncio
async def test_install_full_installs_compatibility_packages_after_scheme_full(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()
    commands = []

    async def fake_run_process(args, success_data=None, exit_codes=None):
        commands.append(args)
        if exit_codes is not None:
            exit_codes.append(0)
        yield service._event("done", {"returncode": 0})

    monkeypatch.setattr(service, "_run_process", fake_run_process)

    events = [event async for event in service.install_full()]

    tlmgr = str(tmp_path / "2025" / "bin" / "x86_64-linux" / "tlmgr")
    assert commands[0] == [tlmgr, "install", "scheme-full"]
    assert commands[1] == [tlmgr, "install", *tex_module.TEX_COMPATIBILITY_PACKAGES]
    assert any("SiGMA LaTeX compatibility packages" in event for event in events)


@pytest.mark.asyncio
async def test_install_full_skips_compatibility_packages_when_scheme_full_fails(tmp_path, monkeypatch):
    _make_tlmgr(tmp_path, "2025")
    monkeypatch.setattr(tex_module, "TEXLIVE_ROOT", str(tmp_path))

    service = TeXService()
    commands = []

    async def fake_run_process(args, success_data=None, exit_codes=None):
        commands.append(args)
        if exit_codes is not None:
            exit_codes.append(1)
        yield service._event("error", {"returncode": 1})

    monkeypatch.setattr(service, "_run_process", fake_run_process)

    events = [event async for event in service.install_full()]

    assert len(commands) == 1
    assert "scheme-full" in commands[0]
    assert not any("SiGMA LaTeX compatibility packages" in event for event in events)
