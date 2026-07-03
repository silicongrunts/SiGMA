"""
TeX Live management service.

The service wraps ``tlmgr`` and TeX Live year selection behind a small
allow-listed API. Output is streamed as SSE records for the settings UI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from collections.abc import AsyncIterator

from app.core.config import DEFAULT_TEXLIVE_YEAR, TEXLIVE_ROOT, UPDATE_TLMGR_BIN
from app.core.logging import get_logger
from app.core.utils import utcnow

logger = get_logger(__name__)

REPOSITORIES = {
    "official": "https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/{year}/tlnet-final",
    "tuna": "https://mirrors.tuna.tsinghua.edu.cn/tex-historic-archive/systems/texlive/{year}/tlnet-final",
}
CURRENT_REPOSITORIES = {
    "official": "https://mirror.ctan.org/systems/texlive/tlnet",
    "tuna": "https://mirrors.tuna.tsinghua.edu.cn/CTAN/systems/texlive/tlnet",
}

PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
SEARCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+*?\\^-]{0,127}$")
YEAR_RE = re.compile(r"^(20[0-9]{2})$")

TEX_BASE_PACKAGES = (
    "latexmk",
    "texcount",
    "synctex",
    "etoolbox",
    "xetex",
)

TEX_COMPATIBILITY_PACKAGES = (
    "collection-langchinese",
    "collection-langcjk",
    "cjk",
    "cjkpunct",
    "ctex",
    "fandol",
    "zhmetrics",
    "arphic",
    "arphic-ttf",
    "latexmk",
    "dvipdfmx",
    "dvips",
    "epstopdf-pkg",
    "pstricks",
    "pst-pdf",
    "auto-pst-pdf",
    "psfrag",
    "psutils",
)


class TeXService:
    """Run TeX Live management commands one at a time."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def get_status(self) -> dict:
        year = self._current_year()
        tlmgr_path = self._tlmgr_bin(year)
        latest_year = self._latest_installed_year()
        return {
            "texlive_root": TEXLIVE_ROOT,
            "current_year": year,
            "latest_installed_year": latest_year,
            "installed_years": self._installed_years(),
            "tlmgr": tlmgr_path,
            "tlmgr_available": os.path.exists(tlmgr_path) or shutil.which("tlmgr") is not None,
            "repositories": self._repository_labels(year),
            "current_repositories": CURRENT_REPOSITORIES,
        }

    async def set_repository(self, repository: str) -> AsyncIterator[str]:
        yield self._event("start", {"operation": "set_repository"})
        year = self._current_year()
        url = self._repository_url(repository, year=year)
        async for event in self._run([self._tlmgr_bin(year), "option", "repository", url]):
            yield event

    async def update(self, repository: str | None = None) -> AsyncIterator[str]:
        yield self._event("start", {"operation": "update"})
        year = self._current_year()
        args = [self._tlmgr_bin(year)]
        if repository:
            args.extend(["--repository", self._repository_url(repository, year=year)])
        args.extend(["update", "--self", "--all"])
        async for event in self._run(args):
            yield event

    async def install_full(self, repository: str | None = None) -> AsyncIterator[str]:
        yield self._event("start", {"operation": "install_full"})
        year = self._current_year()
        full_args = self._tlmgr_install_args(year, ["scheme-full"], repository)
        compatibility_args = self._tlmgr_install_args(year, list(TEX_COMPATIBILITY_PACKAGES), repository)

        if self._lock.locked():
            yield self._event("error", {"message": "Another TeX operation is already running"})
            return

        async with self._lock:
            exit_codes: list[int] = []
            async for event in self._run_process(full_args, exit_codes=exit_codes):
                yield event
            if not exit_codes or exit_codes[-1] != 0:
                return

            yield self._event("log", {"line": "Installing SiGMA LaTeX compatibility packages..."})
            async for event in self._run_process(compatibility_args):
                yield event

    async def install_package(self, package: str, repository: str | None = None) -> AsyncIterator[str]:
        package = package.strip()
        self._validate_package(package)
        yield self._event("start", {"operation": "install_package", "package": package})
        year = self._current_year()
        args = [self._tlmgr_bin(year)]
        if repository:
            args.extend(["--repository", self._repository_url(repository, year=year)])
        args.extend(["install", package])
        async for event in self._run(args):
            yield event

    async def search(self, query: str, repository: str | None = None) -> AsyncIterator[str]:
        query = query.strip()
        if not SEARCH_RE.fullmatch(query):
            raise ValueError("Invalid TeX package search query")
        yield self._event("start", {"operation": "search", "query": query})
        year = self._current_year()
        args = [self._tlmgr_bin(year)]
        if repository:
            args.extend(["--repository", self._repository_url(repository, year=year)])
        args.extend(["search", "--global", query])
        async for event in self._run(args):
            yield event

    async def update_tlmgr(self) -> AsyncIterator[str]:
        yield self._event("start", {"operation": "update_tlmgr"})
        year = self._current_year()
        async for event in self._run([self._update_tlmgr_bin(year), "--update"]):
            yield event

    async def switch_year(self, repository: str | None = None, target_year: str | None = None) -> AsyncIterator[str]:
        current_year = self._current_year()
        next_year = self._validate_target_year(target_year)
        if self._tlmgr_exists(next_year):
            yield self._event("start", {"operation": "switch_year", "target_year": next_year})
            if next_year == current_year:
                yield self._event("done", {
                    "returncode": 0,
                    "target_year": next_year,
                    "message": f"TeX Live {next_year} is already active.",
                })
                return
            if self._lock.locked():
                yield self._event("error", {"message": "Another TeX operation is already running"})
                return
            async with self._lock:
                self._switch_current_year(next_year)
                yield self._event("done", {
                    "returncode": 0,
                    "target_year": next_year,
                    "message": f"Switched TeX Live to {next_year}.",
                })
                return

        target_root = Path(TEXLIVE_ROOT) / next_year
        yield self._event("start", {"operation": "switch_year", "target_year": next_year})
        if self._lock.locked():
            yield self._event("error", {"message": "Another TeX operation is already running"})
            return

        async with self._lock:
            if self._tlmgr_exists(next_year):
                self._switch_current_year(next_year)
                yield self._event("done", {
                    "returncode": 0,
                    "target_year": next_year,
                    "message": f"Switched TeX Live to {next_year}.",
                })
                return

            if target_root.exists():
                yield self._event("log", {"line": f"Removing incomplete TeX Live {next_year} installation..."})
                shutil.rmtree(target_root)

            async for event in self._install_and_activate_year(repository, next_year, target_root):
                yield event

    async def _install_and_activate_year(
        self,
        repository: str | None,
        next_year: str,
        target_root: Path,
    ) -> AsyncIterator[str]:
        url = self._install_year_repository_url(repository or "official", year=next_year)
        work_dir = tempfile.mkdtemp(prefix="sigma-texlive-")
        profile_path = f"{work_dir}/texlive.profile"
        install_dir = f"{work_dir}/install-tl"
        home_dir = str(Path.home())
        quoted_install_dir = shlex.quote(install_dir)
        quoted_profile_path = shlex.quote(profile_path)
        quoted_texlive_root = shlex.quote(TEXLIVE_ROOT)
        base_packages = " ".join(TEX_BASE_PACKAGES)
        script = (
            "set -euo pipefail\n"
            f"mkdir -p {quoted_install_dir}\n"
            f"wget -qO- {shlex.quote(url + '/install-tl-unx.tar.gz')} | tar -xz -C {quoted_install_dir} --strip-components=1\n"
            f"cat > {quoted_profile_path} <<'EOF'\n"
            "selected_scheme scheme-basic\n"
            f"TEXDIR {TEXLIVE_ROOT}/{next_year}\n"
            f"TEXMFCONFIG {home_dir}/.texlive{next_year}/texmf-config\n"
            f"TEXMFHOME {home_dir}/texmf\n"
            f"TEXMFLOCAL {TEXLIVE_ROOT}/texmf-local\n"
            f"TEXMFSYSCONFIG {TEXLIVE_ROOT}/{next_year}/texmf-config\n"
            f"TEXMFSYSVAR {TEXLIVE_ROOT}/{next_year}/texmf-var\n"
            f"TEXMFVAR {home_dir}/.texlive{next_year}/texmf-var\n"
            "binary_x86_64-linux 1\n"
            "instopt_adjustpath 0\n"
            "instopt_adjustrepo 1\n"
            "instopt_letter 0\n"
            "instopt_portable 0\n"
            "tlpdbopt_autobackup 0\n"
            "tlpdbopt_create_formats 1\n"
            "tlpdbopt_desktop_integration 0\n"
            "tlpdbopt_file_assocs 0\n"
            "tlpdbopt_generate_updmap 1\n"
            "tlpdbopt_install_docfiles 0\n"
            "tlpdbopt_install_srcfiles 0\n"
            "EOF\n"
            f"{quoted_install_dir}/install-tl -repository {shlex.quote(url)} -profile {quoted_profile_path}\n"
            f"{quoted_texlive_root}/{next_year}/bin/x86_64-linux/tlmgr option repository {shlex.quote(url)}\n"
            f"{quoted_texlive_root}/{next_year}/bin/x86_64-linux/tlmgr install --repository {shlex.quote(url)} {base_packages}\n"
            f"wget -qO {quoted_texlive_root}/{next_year}/update-tlmgr-latest.sh https://mirror.ctan.org/systems/texlive/tlnet/update-tlmgr-latest.sh\n"
            f"chmod +x {quoted_texlive_root}/{next_year}/update-tlmgr-latest.sh\n"
            f"ln -sfn {shlex.quote(next_year)} {quoted_texlive_root}/current\n"
        )
        exit_codes: list[int] = []
        try:
            async for event in self._run_process(
                ["bash", "-lc", script],
                success_data={
                    "target_year": next_year,
                    "message": f"TeX Live {next_year} installed and activated.",
                },
                exit_codes=exit_codes,
            ):
                yield event
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if (not exit_codes or exit_codes[-1] != 0) and target_root.exists() and not self._tlmgr_exists(next_year):
                shutil.rmtree(target_root, ignore_errors=True)

    async def _run(self, args: list[str], success_data: dict | None = None) -> AsyncIterator[str]:
        if self._lock.locked():
            yield self._event("error", {"message": "Another TeX operation is already running"})
            return

        async with self._lock:
            async for event in self._run_process(args, success_data=success_data):
                yield event

    async def _run_process(
        self,
        args: list[str],
        success_data: dict | None = None,
        exit_codes: list[int] | None = None,
    ) -> AsyncIterator[str]:
        logger.info("Running TeX command: %s", " ".join(args))
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            yield self._event("error", {"message": f"Command not found: {args[0]}"})
            logger.warning("TeX command not found: %s", args[0], exc_info=True)
            return

        try:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                yield self._event("log", {"line": line.decode("utf-8", errors="replace").rstrip()})

            code = await process.wait()
            if exit_codes is not None:
                exit_codes.append(code)
            event = "done" if code == 0 else "error"
            payload = {"returncode": code}
            if code == 0 and success_data:
                payload.update(success_data)
            yield self._event(event, payload)
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            logger.info("TeX command cancelled: %s", " ".join(args))
            raise

    def _tlmgr_install_args(self, year: str, packages: list[str], repository: str | None = None) -> list[str]:
        args = [self._tlmgr_bin(year)]
        if repository:
            args.extend(["--repository", self._repository_url(repository, year=year)])
        args.extend(["install", *packages])
        return args

    def _installed_years(self) -> list[str]:
        root = Path(TEXLIVE_ROOT)
        if not root.exists():
            return []
        years = []
        for child in root.iterdir():
            if child.is_dir() and YEAR_RE.fullmatch(child.name) and self._tlmgr_exists(child.name):
                years.append(child.name)
        return sorted(years)

    def _current_year(self) -> str:
        linked_year = self._current_link_year()
        if linked_year and self._tlmgr_exists(linked_year):
            return linked_year
        latest_year = self._latest_installed_year()
        if latest_year:
            return latest_year
        return DEFAULT_TEXLIVE_YEAR

    def _latest_installed_year(self) -> str:
        years = self._installed_years()
        if years:
            return years[-1]
        return ""

    def _current_link_year(self) -> str:
        current_path = Path(TEXLIVE_ROOT) / "current"
        try:
            target_path = current_path.resolve(strict=True)
        except FileNotFoundError:
            return ""
        if target_path.parent != Path(TEXLIVE_ROOT).resolve():
            return ""
        if YEAR_RE.fullmatch(target_path.name):
            return target_path.name
        return ""

    def _switch_current_year(self, year: str) -> None:
        if not self._tlmgr_exists(year):
            raise ValueError(f"TeX Live {year} is not installed")
        root = Path(TEXLIVE_ROOT)
        temp_link = root / f".current-{year}.tmp"
        current_link = root / "current"
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
        temp_link.symlink_to(year)
        temp_link.replace(current_link)

    def _tlmgr_exists(self, year: str) -> bool:
        return Path(self._tlmgr_path(year)).exists()

    def _tlmgr_path(self, year: str) -> str:
        return f"{TEXLIVE_ROOT}/{year}/bin/x86_64-linux/tlmgr"

    def _tlmgr_bin(self, year: str) -> str:
        path = self._tlmgr_path(year)
        if os.path.exists(path):
            return path
        return shutil.which("tlmgr") or path

    def _update_tlmgr_bin(self, year: str) -> str:
        path = f"{TEXLIVE_ROOT}/{year}/update-tlmgr-latest.sh"
        if os.path.exists(path):
            return path
        return UPDATE_TLMGR_BIN or path

    def _repository_labels(self, year: str) -> dict[str, str]:
        return {key: value.format(year=year) for key, value in REPOSITORIES.items()}

    def _repository_url(self, repository: str, *, year: str, use_current: bool = False) -> str:
        repositories = CURRENT_REPOSITORIES if use_current else self._repository_labels(year)
        if repository in repositories:
            return repositories[repository]
        if repository.startswith(("https://", "http://")) and "/systems/texlive/" in repository:
            return repository
        raise ValueError("Unsupported TeX Live repository")

    def _install_year_repository_url(self, repository: str, *, year: str) -> str:
        use_current_tlnet = int(year) >= utcnow().year
        return self._repository_url(repository, year=year, use_current=use_current_tlnet)

    @staticmethod
    def _validate_package(package: str) -> None:
        if not PACKAGE_RE.fullmatch(package):
            raise ValueError("Invalid TeX package name")

    def _validate_target_year(self, target_year: str | None) -> str:
        year = (target_year or str(utcnow().year)).strip()
        if not YEAR_RE.fullmatch(year):
            raise ValueError("Invalid TeX Live target year")
        return year

    @staticmethod
    def _event(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=True)}\n\n"


tex_service = TeXService()
