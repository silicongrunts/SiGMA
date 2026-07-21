import re
import gzip
import asyncio
import os
import platform
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional, Dict, Any, List

from app.core.config import settings
from app.core.exceptions import (
    LaTeXCompilationError, SyncTeXError, FileMissingError,
    InvalidPathError, ProjectNotFoundError,
)
from app.core.logging import get_logger
from app.services.tex_service import TEXLIVE_ROOT

logger = get_logger(__name__)

TEX_EXTS = {"tex", "cls", "sty", "bib"}
LATEX_OUTPUT_BASE = "output"
LATEX_KEEP_OUTPUTS = {"output.pdf", "output.synctex.gz"}
LATEX_EXTRA_TEMP_FILES = {"missfont.log", "texput.log"}
LATEXMK_TIMEOUT_SECONDS = 120
LATEXMK_COMPILER_FLAGS = {
    "latex": "-pdfdvi",
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
    "lualatex": "-lualatex",
}

# Per-project compile locks (module-level singleton)
_compile_locks: dict[str, asyncio.Lock] = {}


def _get_compile_lock(project_id: str) -> asyncio.Lock:
    return _compile_locks.setdefault(project_id, asyncio.Lock())


class LaTeXService:
    """Service for compiling LaTeX documents in the source directory."""

    def __init__(self):
        self.available_engines = settings.LATEX_ENGINES

    def _is_safe_filename(self, filename: str) -> bool:
        if not filename or filename.startswith(("/", ".", "\\")) or "\\" in filename:
            return False
        path = PurePosixPath(filename)
        if path.is_absolute():
            return False
        if any(part in ("", ".", "..") or part.startswith(".") for part in path.parts):
            return False
        return bool(re.match(r'^[a-zA-Z0-9_\-\./]+$', filename))

    async def compile(self, project_id: str, main_file: Optional[str] = None, engine: Optional[str] = None) -> Dict[str, Any]:
        project_path = settings.get_project_path(project_id).resolve()

        # Load project defaults if params not provided
        from app.services.project_service import project_service
        p_meta = await project_service.get_project(project_id)

        main_file = main_file or p_meta.get("main_file", "main.tex")
        engine = engine or p_meta.get("engine", settings.DEFAULT_LATEX_ENGINE)
        if engine not in settings.LATEX_ENGINES:
            raise LaTeXCompilationError(f"Unsupported LaTeX engine: {engine}")

        if not self._is_safe_filename(main_file):
            raise InvalidPathError(main_file)
        if not (project_path / main_file).exists():
            raise FileMissingError(f"Main file {main_file}")

        main_path = project_path / main_file
        compile_dir = main_path.parent

        return await self._compile_latexmk_project(
            compile_dir, main_path.name, main_file, engine
        )

    async def _compile_latexmk_project(
        self,
        compile_dir: Path,
        main_name: str,
        main_file: str,
        engine: str,
    ) -> Dict[str, Any]:
        backups: dict[Path, Path] = {}
        keep_outputs: set[str] = set()
        restored = False
        try:
            backups = self._backup_existing_temp_outputs(compile_dir)
            keep_outputs = self._snapshot_keep_outputs(compile_dir)
            # Snapshot the PDF's mtime BEFORE running latexmk so the finalizer
            # can tell a freshly written PDF apart from one left over from a
            # previous successful compile (latexmk -f preserves the old file
            # when the current run fails outright).
            prior_pdf_mtime = self._pdf_mtime(compile_dir)
            success, stdout, stderr = await self._run_latexmk(
                compile_dir, main_name, engine
            )
            log_content = self._latexmk_log_content(compile_dir, stdout, stderr)
            self._cleanup_latex_outputs(compile_dir)
            self._restore_temp_output_backups(backups)
            restored = True

            result = self._finalize_pdf_compile(
                success,
                compile_dir,
                self._relative_output_pdf(main_file),
                log_content,
                main_file,
                prior_pdf_mtime,
            )
            if not result.get("success") and not result.get("pdf_path"):
                self._cleanup_failed_keep_outputs(compile_dir, keep_outputs)
            return result
        finally:
            if not restored:
                self._cleanup_latex_outputs(compile_dir)
                self._restore_temp_output_backups(backups)
            self._discard_temp_output_backups(backups)

    def _finalize_pdf_compile(
        self,
        compilation_successful: bool,
        compile_dir: Path,
        output_path: str,
        log_content: str,
        main_file: str,
        prior_pdf_mtime: Optional[int],
    ) -> Dict[str, Any]:
        synctex_file = compile_dir / f"{LATEX_OUTPUT_BASE}.synctex.gz"
        pdf_file = compile_dir / f"{LATEX_OUTPUT_BASE}.pdf"

        # Classify the PDF in one pass: ``produced_this_run`` rejects a file
        # left over from a previous successful compile when the current run
        # failed outright (latexmk -f preserves the old file). ``valid`` adds
        # the size and magic-header checks that mark a file as actually usable.
        try:
            st = pdf_file.stat()
            produced_this_run = st.st_mtime_ns != prior_pdf_mtime
            size = st.st_size
        except OSError:
            produced_this_run = False
            size = 0
        valid = (
            produced_this_run
            and size >= 1000
            and self._has_pdf_magic_header(pdf_file)
        )

        # Sanitize synctex whenever a usable PDF is exposed, so source↔PDF
        # jumps work in both the clean and the compiled-with-errors cases.
        if valid and synctex_file.exists():
            self._sanitize_synctex(synctex_file, str(compile_dir))

        if compilation_successful and valid:
            return {
                "success": True, "log": log_content,
                "pdf_path": output_path,
                "diagnostics": [],
            }

        diagnostics = self._parse_diagnostics(log_content, main_file)

        # Errors present but a usable PDF was still produced (latexmk -f keeps
        # going past non-fatal errors). Surface diagnostics AND the pdf_path so
        # the frontend can refresh the preview while still flagging the log.
        if valid:
            return {
                "success": False, "log": log_content,
                "error": "Compilation completed with errors",
                "pdf_path": output_path,
                "diagnostics": diagnostics,
            }

        # No usable PDF this run. ``produced_this_run`` distinguishes a corrupt
        # output written this run from a file that latexmk never touched.
        if not produced_this_run:
            error_msg = "PDF not generated"
        elif size < 1000:
            error_msg = "Generated PDF is too small (likely corrupted)"
        else:
            error_msg = "Invalid PDF generated"
        return {"success": False, "log": log_content, "error": error_msg, "diagnostics": diagnostics}

    @staticmethod
    def _pdf_mtime(compile_dir: Path) -> Optional[int]:
        """Return the PDF's mtime in nanoseconds, or None if it does not exist."""
        pdf_file = compile_dir / f"{LATEX_OUTPUT_BASE}.pdf"
        try:
            return pdf_file.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _has_pdf_magic_header(pdf_file: Path) -> bool:
        with open(pdf_file, "rb") as f:
            return f.read(4) == b"%PDF"

    # ------------------------------------------------------------------
    # Log parsing
    # ------------------------------------------------------------------

    def _parse_diagnostics(self, log: str, main_file: str) -> List[Dict[str, Any]]:
        """Parse a LaTeX log into structured diagnostics.

        Extracts errors (``!`` lines) and warnings with their source
        file and line number so the frontend can place gutter marks
        and underlines in the editor.

        Returns a list of dicts:
            ``{"file": str, "line": int, "severity": str, "message": str}``
        """
        diagnostics: List[Dict[str, Any]] = []
        lines = log.splitlines()

        current_file = main_file
        file_stack: List[str | None] = []

        i = 0
        while i < len(lines):
            raw = lines[i]

            current_file = self._update_log_file_context(raw, current_file, file_stack)

            # --- Errors: ! <message> ... l.<line> ---
            if raw.startswith('!'):
                msg = raw[2:].strip()
                # Look ahead for the line number (l.<number>)
                line_no = None
                for j in range(i + 1, min(i + 6, len(lines))):
                    m = re.match(r'l\.(\d+)', lines[j].strip())
                    if m:
                        line_no = int(m.group(1))
                        break
                if line_no is not None:
                    diagnostics.append({
                        "file": current_file,
                        "line": line_no,
                        "severity": "error",
                        "message": msg[:300],
                    })

            # --- Warnings: ... on input line <n> ---
            elif 'Warning' in raw:
                m = re.search(r'on input line (\d+)', raw)
                if m:
                    # Extract the warning message (before "on input line")
                    warn_msg = raw.split('Warning')[0] + 'Warning'
                    warn_detail = re.sub(r'^.*?Warning[:\s]*', '', raw, count=1)
                    if warn_detail:
                        full_msg = warn_detail.split('on input line')[0].strip()
                    else:
                        full_msg = raw.strip()
                    diagnostics.append({
                        "file": current_file,
                        "line": int(m.group(1)),
                        "severity": "warning",
                        "message": full_msg[:300],
                    })

            # --- Bad boxes: Overfull/Underfull ... at lines <n>--<m> ---
            elif re.match(r'(Overfull|Underfull) \\[vh]box', raw):
                m = re.search(r'at lines? (\d+)', raw)
                if m:
                    box_type = 'warning'
                    diagnostics.append({
                        "file": current_file,
                        "line": int(m.group(1)),
                        "severity": box_type,
                        "message": raw.strip()[:300],
                    })

            i += 1

        return diagnostics

    def _update_log_file_context(
        self,
        raw: str,
        current_file: str,
        file_stack: List[str | None],
    ) -> str:
        index = 0
        while index < len(raw):
            char = raw[index]
            if char == "(":
                token = self._latex_log_open_token(raw, index + 1)
                if token:
                    file_stack.append(current_file)
                    current_file = token
                    index += len(token) + 1
                else:
                    file_stack.append(None)
                    index += 1
                continue
            if char == ")":
                if file_stack:
                    previous_file = file_stack.pop()
                    if previous_file is not None:
                        current_file = previous_file
                index += 1
                continue
            index += 1
        return current_file

    def _latex_log_open_token(self, raw: str, start: int) -> str:
        end = start
        while end < len(raw) and not raw[end].isspace() and raw[end] not in "()":
            end += 1
        token = raw[start:end].strip()
        if not token:
            return ""
        clean_token = token[2:] if token.startswith("./") else token
        ext = clean_token.rsplit(".", 1)[-1].lower() if "." in clean_token else ""
        if ext in TEX_EXTS:
            return clean_token
        return ""

    async def _run_latexmk(
        self,
        compile_dir: Path,
        main_name: str,
        engine: str,
    ):
        compiler_flag = LATEXMK_COMPILER_FLAGS[engine]
        cmd = [
            "latexmk",
            "-cd",
            f"-jobname={LATEX_OUTPUT_BASE}",
            "-synctex=1",
            "-interaction=batchmode",
            "-time",
            "-f",
            compiler_flag,
            main_name,
        ]
        env = self._latexmk_env()
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(compile_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return False, "", "latexmk command not found. Install latexmk from the TeX Live manager."
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=LATEXMK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await self._terminate_latexmk(process)
            return False, "", f"latexmk timed out after {LATEXMK_TIMEOUT_SECONDS} seconds"
        except asyncio.CancelledError:
            await self._terminate_latexmk(process)
            raise
        stdout_text = stdout.decode('utf-8', 'ignore')
        stderr_text = stderr.decode('utf-8', 'ignore')
        success = process.returncode == 0
        return success, stdout_text, stderr_text

    def _latexmk_env(self) -> dict[str, str]:
        env = os.environ.copy()
        path_parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        texlive_bin_dirs = self._texlive_bin_dirs()
        missing_bin_dirs = [path for path in texlive_bin_dirs if path not in path_parts]
        if missing_bin_dirs:
            env["PATH"] = os.pathsep.join([*missing_bin_dirs, *path_parts])
        return env

    def _texlive_bin_dirs(self) -> list[str]:
        bin_root = Path(TEXLIVE_ROOT) / "current" / "bin"
        if not bin_root.is_dir():
            return []
        bin_dirs = sorted(path for path in bin_root.iterdir() if path.is_dir())
        platform_names = self._texlive_platform_names()
        preferred = [path for path in bin_dirs if path.name in platform_names]
        fallback = [path for path in bin_dirs if path.name not in platform_names]
        return [str(path) for path in [*preferred, *fallback]]

    def _texlive_platform_names(self) -> set[str]:
        machine = platform.machine().lower()
        if machine in {"x86_64", "amd64"}:
            return {"x86_64-linux"}
        if machine in {"aarch64", "arm64"}:
            return {"aarch64-linux"}
        if machine.startswith("arm"):
            return {"armhf-linux", "armel-linux"}
        return set()

    async def _terminate_latexmk(self, process: asyncio.subprocess.Process) -> None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    def _latexmk_log_content(self, compile_dir: Path, stdout: str, stderr: str) -> str:
        parts = ["--- LATEXMK STDOUT ---", stdout, "--- LATEXMK STDERR ---", stderr]
        latex_log = compile_dir / f"{LATEX_OUTPUT_BASE}.log"
        if latex_log.exists():
            parts.extend([
                "--- LATEX LOG ---",
                latex_log.read_text(encoding="utf-8", errors="replace"),
            ])
        return "\n".join(parts)

    def _latex_output_artifacts(self, compile_dir: Path) -> list[Path]:
        artifacts = list(compile_dir.glob(f"{LATEX_OUTPUT_BASE}.*"))
        artifacts.extend(compile_dir / name for name in LATEX_EXTRA_TEMP_FILES)
        return artifacts

    def _snapshot_keep_outputs(self, compile_dir: Path) -> set[str]:
        return {
            name
            for name in LATEX_KEEP_OUTPUTS
            if (compile_dir / name).is_file() or (compile_dir / name).is_symlink()
        }

    def _backup_existing_temp_outputs(self, compile_dir: Path) -> dict[Path, Path]:
        backup_dir = Path(tempfile.mkdtemp(prefix=".sigma-latex-backup-", dir=compile_dir))
        backups: dict[Path, Path] = {}
        try:
            for artifact in self._latex_output_artifacts(compile_dir):
                if artifact.name in LATEX_KEEP_OUTPUTS:
                    continue
                if artifact.is_file() or artifact.is_symlink():
                    backup_path = backup_dir / artifact.name
                    artifact.replace(backup_path)
                    backups[artifact] = backup_path
            if not backups:
                shutil.rmtree(backup_dir, ignore_errors=True)
            return backups
        except OSError:
            self._restore_temp_output_backups(backups)
            shutil.rmtree(backup_dir, ignore_errors=True)
            raise

    def _restore_temp_output_backups(self, backups: dict[Path, Path]) -> None:
        for artifact, backup_path in backups.items():
            if artifact.exists() or artifact.is_symlink():
                continue
            if backup_path.exists() or backup_path.is_symlink():
                backup_path.replace(artifact)

    def _discard_temp_output_backups(self, backups: dict[Path, Path]) -> None:
        backup_dirs = {backup_path.parent for backup_path in backups.values()}
        for backup_dir in backup_dirs:
            shutil.rmtree(backup_dir, ignore_errors=True)

    def _cleanup_latex_outputs(self, compile_dir: Path) -> None:
        for artifact in self._latex_output_artifacts(compile_dir):
            if artifact.name in LATEX_KEEP_OUTPUTS:
                continue
            if artifact.is_file() or artifact.is_symlink():
                try:
                    artifact.unlink()
                except OSError:
                    logger.warning("Failed to remove LaTeX artifact %s", artifact, exc_info=True)

    def _cleanup_failed_keep_outputs(self, compile_dir: Path, existing_outputs: set[str]) -> None:
        for name in LATEX_KEEP_OUTPUTS:
            if name in existing_outputs:
                continue
            artifact = compile_dir / name
            if artifact.is_file() or artifact.is_symlink():
                try:
                    artifact.unlink()
                except OSError:
                    logger.warning("Failed to remove failed LaTeX output %s", artifact, exc_info=True)

    def _relative_output_pdf(self, main_file: str) -> str:
        parent = PurePosixPath(main_file).parent
        if str(parent) == ".":
            return f"{LATEX_OUTPUT_BASE}.pdf"
        return str(parent / f"{LATEX_OUTPUT_BASE}.pdf")

    @staticmethod
    def _sanitize_synctex(synctex_path: Path, project_path: str):
        """Rewrite synctex Input paths to absolute.

        pdflatex records paths relative to its cwd (project_path), but the
        synctex CLI resolves relative paths from the synctex file's own
        directory. Converting to absolute paths makes source
        resolution independent of where the CLI is invoked.
        """
        project_path = project_path.rstrip('/')
        try:
            with gzip.open(synctex_path, 'rb') as f:
                content = f.read().decode('utf-8', errors='ignore')

            def _absolutize(m):
                prefix = m.group(1)
                path = m.group(2)
                if path.startswith('/'):
                    return m.group(0)
                return f"{prefix}{project_path}/{path}"

            sanitized = re.sub(
                r'^(Input:\d+:)(.+)',
                _absolutize,
                content,
                flags=re.MULTILINE,
            )
            with gzip.open(synctex_path, 'wb') as f:
                f.write(sanitized.encode('utf-8'))
        except Exception:
            logger.warning("Failed to sanitize SyncTeX file %s; forward/inverse search may be inaccurate", synctex_path, exc_info=True)

    async def run_synctex(self, project_id: str, data: Any) -> Dict[str, Any]:
        project_path = settings.get_project_path(project_id).resolve()

        from app.services.project_service import project_service
        p_meta = await project_service.get_project(project_id)
        main_file = p_meta.get("main_file", "main.tex")
        if not self._is_safe_filename(main_file):
            raise SyncTeXError(f"Invalid main file: {main_file}")
        compile_dir = (project_path / main_file).parent
        pdf_path = compile_dir / f"{LATEX_OUTPUT_BASE}.pdf"

        if not pdf_path.exists():
            raise SyncTeXError("PDF not found")

        mode = data.type
        if mode == "forward":
            target_file = data.file or main_file
            if not self._is_safe_filename(target_file):
                raise SyncTeXError(f"Invalid file: {target_file}")
            target_path = (project_path / target_file).resolve()
            try:
                target_path.relative_to(project_path)
            except ValueError:
                raise SyncTeXError(f"Invalid file: {target_file}")
            source_arg = str(target_path)
            cmd = ["synctex", "view", "-i", f"{data.line or 1}:{data.column or 0}:{source_arg}", "-o", str(pdf_path)]
        elif mode == "backward":
            cmd = ["synctex", "edit", "-o", f"{data.page or 1}:{data.x or 0}:{data.y or 0}:{str(pdf_path)}"]
        else:
            raise SyncTeXError(f"Unsupported SyncTeX type: {mode}")

        try:
            process = await asyncio.create_subprocess_exec(*cmd, cwd=str(compile_dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await process.communicate()
            output = stdout.decode('utf-8')
            if process.returncode != 0:
                stderr_text = stderr.decode('utf-8', 'ignore')
                raise SyncTeXError(stderr_text.strip() or "SyncTeX command failed")
            if mode == "forward":
                records = self._parse_synctex_output(output)
                for record in records:
                    res = {"success": True}
                    if "Page" in record:
                        res["page"] = int(record["Page"])
                    if "x" in record:
                        res["x"] = float(record["x"])
                    if "y" in record:
                        res["y"] = float(record["y"])
                    if "h" in record:
                        res["x"] = float(record["h"])
                    if "v" in record:
                        res["y"] = float(record["v"])
                    if "page" in res and "x" in res and "y" in res:
                        return res
                raise SyncTeXError("No PDF location found")
            else:
                records = self._parse_synctex_output(output)
                for record in records:
                    source_file = self._source_file_from_synctex_record(
                        record, project_path, compile_dir,
                    )
                    if not source_file:
                        continue
                    return {
                        "success": True,
                        "file": source_file,
                        "line": int(record["Line"]),
                    }
                raise SyncTeXError("No source location found")
        except SyncTeXError:
            raise
        except Exception as e:
            raise SyncTeXError(str(e))

    async def synctex(self, project_id: str, data: Any) -> Dict[str, Any]:
        return await self.run_synctex(project_id, data)

    def _parse_synctex_output(self, output: str) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        current_record: dict[str, str] | None = None
        for raw_line in output.splitlines():
            label, value = self._split_synctex_line(raw_line)
            if label == "Output":
                current_record = {}
                records.append(current_record)
                continue
            if current_record is None:
                continue
            if label:
                current_record[label] = value
        return records

    def _split_synctex_line(self, line: str) -> tuple[str, str]:
        separator_index = line.find(":")
        if separator_index == -1:
            return "", line
        return line[:separator_index].strip(), line[separator_index + 1:].strip()

    def _source_file_from_synctex_record(
        self,
        record: dict[str, str],
        project_path: Path,
        compile_dir: Path,
    ) -> str | None:
        if "Input" not in record or "Line" not in record:
            return None
        try:
            line_no = int(record["Line"])
        except ValueError:
            return None
        if line_no < 1:
            return None

        found_path = Path(record["Input"])
        full_found_path = found_path if found_path.is_absolute() else (compile_dir / found_path).resolve()
        try:
            relative_file = full_found_path.relative_to(project_path)
        except ValueError:
            return None
        if relative_file.suffix.lower().lstrip(".") not in TEX_EXTS:
            return None
        if not full_found_path.is_file():
            return None
        return str(relative_file)

    def get_pdf_path(self, project_id: str, filename: str = "output.pdf") -> Path:
        """Resolve and validate a PDF path. Raises exceptions on failure."""
        project_path = settings.get_project_path(project_id).resolve()
        if not project_path.exists():
            raise ProjectNotFoundError(project_id)
        if not self._is_safe_filename(filename):
            raise FileMissingError(filename)
        pdf_path = (project_path / filename).resolve()
        try:
            pdf_path.relative_to(project_path)
        except ValueError:
            raise FileMissingError(filename)
        if not pdf_path.exists() or not pdf_path.is_file():
            raise FileMissingError(filename)
        return pdf_path

    async def get_compile_status(self, project_id: str) -> dict:
        """Check compilation status. Returns {has_pdf, pdf_files}."""
        project_path = settings.get_project_path(project_id).resolve()
        if not project_path.exists():
            return {"has_pdf": False, "pdf_files": []}

        pdf_name = f"{LATEX_OUTPUT_BASE}.pdf"
        pdf_filename = pdf_name
        try:
            pdf_filename = await self.get_pdf_filename(project_id)
        except Exception:
            logger.warning("Failed to determine compile status PDF path for %s", project_id, exc_info=True)

        if not self._is_safe_filename(pdf_filename):
            return {"has_pdf": False, "pdf_files": []}

        pdf_file = (project_path / pdf_filename).resolve()
        try:
            pdf_file.relative_to(project_path)
        except ValueError:
            return {"has_pdf": False, "pdf_files": []}

        pdf_files = [pdf_filename] if pdf_file.exists() and pdf_file.is_file() else []
        return {"has_pdf": len(pdf_files) > 0, "pdf_files": pdf_files}

    # ------------------------------------------------------------------
    # Orchestration: main-file resolution + project-level compile lock
    # ------------------------------------------------------------------

    def resolve_main_file(self, project_id: str, main_file: str) -> str | None:
        """If *main_file* doesn't exist at its given path, search subdirectories
        for a file with the same basename and return the corrected relative path.

        Returns ``None`` if not found anywhere.
        """
        project_path = settings.get_project_path(project_id).resolve()
        if (project_path / main_file).exists():
            return main_file

        basename = Path(main_file).name
        candidates = []
        for candidate in project_path.rglob(basename):
            if any(part.startswith('.') for part in candidate.relative_to(project_path).parts):
                continue
            candidates.append(str(candidate.relative_to(project_path)))
        if len(candidates) > 1:
            choices = ", ".join(sorted(candidates)[:5])
            raise LaTeXCompilationError(
                f"Multiple files named {basename} were found. Please select the main TeX file explicitly: {choices}"
            )
        if candidates:
            return candidates[0]
        return None

    async def compile_project(
        self, project_id: str, request_main_file: str = "", engine: str = "",
    ) -> dict:
        """Resolve the main file and compile the project.

        Returns the unified result dict (success/error/log/diagnostics).
        """
        from app.services.project_service import project_service

        lock = _get_compile_lock(project_id)
        async with lock:
            main_file = request_main_file
            if not main_file:
                try:
                    proj = await project_service.get_project(project_id)
                    main_file = proj.get("main_file") or ""
                except Exception:
                    logger.debug("Failed to read project main_file for %s", project_id, exc_info=True)

            if not main_file:
                return {
                    "success": False,
                    "error": "No main TeX file configured for this project",
                    "log": "",
                    "diagnostics": [],
                }

            ext = main_file.rsplit(".", 1)[-1].lower() if "." in main_file else ""
            if ext not in TEX_EXTS:
                return {
                    "success": False,
                    "error": f"Cannot compile '{main_file}': not a TeX file",
                    "log": "",
                    "diagnostics": [],
                }

            try:
                resolved = self.resolve_main_file(project_id, main_file)
            except LaTeXCompilationError as exc:
                return {
                    "success": False,
                    "error": exc.message,
                    "log": "",
                    "diagnostics": [],
                }
            if resolved and resolved != main_file:
                try:
                    await project_service.update_project(project_id, {"main_file": resolved})
                except Exception:
                    logger.debug("Failed to update resolved main_file for %s", project_id, exc_info=True)
                main_file = resolved

            return await self.compile(
                project_id=project_id, main_file=main_file, engine=engine,
            )

    async def get_pdf_filename(self, project_id: str) -> str:
        """Determine the PDF filename for a project (used by the PDF endpoint)."""
        try:
            from app.services.project_service import project_service
            proj = await project_service.get_project(project_id)
            main_file = proj.get("main_file", "")
            if main_file:
                return self._relative_output_pdf(main_file)
        except Exception:
            logger.warning("Failed to determine PDF filename for %s", project_id, exc_info=True)
        return f"{LATEX_OUTPUT_BASE}.pdf"


latex_service = LaTeXService()
