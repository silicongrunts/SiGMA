"""
Tests for latex_service.get_pdf_filename() (migrated from compile_service).
"""

import asyncio
import gzip
import signal
from unittest.mock import patch, AsyncMock

import pytest

from app.services.latex_service import LaTeXService, latex_service
import app.services.latex_service as latex_service_module
import app.services.project_service as project_service_module


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_pdf_filename_from_paper_tex():
    """Returns output.pdf when project main_file is paper.tex."""
    with patch.object(
        project_service_module.project_service, "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "paper.tex"},
    ):
        result = await latex_service.get_pdf_filename("any-id")
    assert result == "output.pdf"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_pdf_filename_no_main_file():
    """Returns output.pdf when project has no main_file."""
    with patch.object(
        project_service_module.project_service, "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": ""},
    ):
        result = await latex_service.get_pdf_filename("any-id")
    assert result == "output.pdf"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_pdf_filename_nested_main_file():
    """Returns correct output PDF path for nested main_file paths."""
    with patch.object(
        project_service_module.project_service, "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await latex_service.get_pdf_filename("any-id")
    assert result == "chapters/output.pdf"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_pdf_filename_exception_fallback():
    """Returns output.pdf when get_project raises (project not found etc.)."""
    with patch.object(
        project_service_module.project_service, "get_project",
        new_callable=AsyncMock,
        side_effect=Exception("db error"),
    ):
        result = await latex_service.get_pdf_filename("any-id")
    assert result == "output.pdf"


@pytest.mark.parametrize(
    ("engine", "compiler_flag"),
    [
        ("pdflatex", "-pdf"),
        ("latex", "-pdfdvi"),
        ("xelatex", "-xelatex"),
        ("lualatex", "-lualatex"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_engines_use_latexmk_flags(tmp_path, monkeypatch, engine, compiler_flag):
    """Compilation uses latexmk engine flags for every supported engine."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    calls = []

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        cmd = [
            "latexmk",
            "-cd",
            "-jobname=output",
            "-synctex=1",
            "-interaction=batchmode",
            "-time",
            "-f",
            latex_service_module.LATEXMK_COMPILER_FLAGS[engine_arg],
            main_name,
        ]
        calls.append(cmd)
        (compile_dir_arg / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
        return True, "latexmk ok", ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": engine},
    ):
        result = await service.compile("x")

    assert result["success"] is True
    assert result["pdf_path"] == "output.pdf"
    assert calls[0][0] == "latexmk"
    assert "-jobname=output" in calls[0]
    assert not any(arg.startswith("-outdir=") or arg.startswith("-auxdir=") for arg in calls[0])
    assert compiler_flag in calls[0]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_returns_pdf_path_when_errors_but_pdf_usable(tmp_path, monkeypatch):
    """Errors present but a usable PDF was produced → success=False with pdf_path."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    # A log containing an Undefined control sequence error so _parse_diagnostics
    # yields at least one diagnostic entry.
    error_log = (
        "! Undefined control sequence.\n"
        "l.5 \\fakemacro\n"
    )

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        (compile_dir_arg / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
        return False, error_log, ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is False
    assert result["pdf_path"] == "output.pdf"
    assert result["error"] == "Compilation completed with errors"
    assert any(d["severity"] == "error" for d in result["diagnostics"])


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_no_pdf_path_when_errors_and_no_pdf(tmp_path, monkeypatch):
    """Errors present and no PDF produced → success=False without pdf_path."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    error_log = "! Undefined control sequence.\nl.5 \\fakemacro\n"

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        return False, error_log, ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is False
    assert "pdf_path" not in result
    assert result["error"] == "PDF not generated"
    assert any(d["severity"] == "error" for d in result["diagnostics"])


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_failed_compile_with_usable_pdf_keeps_pdf(tmp_path, monkeypatch):
    """A failed compile that still produced a usable PDF must NOT delete it."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    error_log = "! Undefined control sequence.\nl.5 \\fakemacro\n"

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        (compile_dir_arg / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
        return False, error_log, ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is False
    assert result.get("pdf_path") == "output.pdf"
    # The freshly produced PDF survives the post-compile cleanup so the
    # frontend can actually fetch it.
    assert (project_path / "output.pdf").exists()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_no_pdf_path_when_previous_pdf_left_untouched(tmp_path, monkeypatch):
    """A failed run that leaves a previous successful PDF on disk must NOT
    report pdf_path — otherwise the frontend would refresh the preview with a
    stale PDF and skip the error modal. Regression for the case where the user
    clears a working document and recompiles something that fails outright.
    """
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    # Simulate a PDF left over from a PRIOR successful compile. fake_run_latexmk
    # below does NOT touch it, so its mtime stays equal to the pre-compile
    # snapshot and the finalizer must classify it as "not produced this run".
    stale_pdf = project_path / "output.pdf"
    stale_pdf.write_bytes(b"%PDF" + b"x" * 1200)
    error_log = "! Undefined control sequence.\nl.5 \\fakemacro\n"

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        # Failed run — no PDF written.
        return False, error_log, ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is False
    assert "pdf_path" not in result
    assert result["error"] == "PDF not generated"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_runs_in_nested_main_file_directory(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    calls = []

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        calls.append((compile_dir_arg, main_name, engine_arg))
        (compile_dir_arg / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
        return True, "latexmk ok", ""

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()
    monkeypatch.setattr(service, "_run_latexmk", fake_run_latexmk)

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is True
    assert result["pdf_path"] == "chapters/output.pdf"
    assert calls == [(source_dir, "main.tex", "pdflatex")]


def test_cleanup_latex_outputs_keeps_pdf_synctex_and_removes_new_temp_files(tmp_path):
    service = LaTeXService()
    (tmp_path / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
    (tmp_path / "output.synctex.gz").write_bytes(b"sync")
    (tmp_path / "output.aux").write_text("new", encoding="utf-8")
    (tmp_path / "output.log").write_text("new", encoding="utf-8")
    (tmp_path / "output.fls").write_text("new", encoding="utf-8")
    (tmp_path / "missfont.log").write_text("new", encoding="utf-8")

    service._cleanup_latex_outputs(tmp_path)

    assert (tmp_path / "output.pdf").exists()
    assert (tmp_path / "output.synctex.gz").exists()
    assert not (tmp_path / "output.aux").exists()
    assert not (tmp_path / "output.log").exists()
    assert not (tmp_path / "output.fls").exists()
    assert not (tmp_path / "missfont.log").exists()


def test_temp_output_backups_preserve_existing_temp_files(tmp_path):
    service = LaTeXService()
    (tmp_path / "output.aux").write_text("old", encoding="utf-8")
    (tmp_path / "output.log").write_text("old log", encoding="utf-8")

    backups = service._backup_existing_temp_outputs(tmp_path)

    assert not (tmp_path / "output.aux").exists()
    assert not (tmp_path / "output.log").exists()

    (tmp_path / "output.aux").write_text("new", encoding="utf-8")
    (tmp_path / "output.fls").write_text("new", encoding="utf-8")
    service._cleanup_latex_outputs(tmp_path)
    service._restore_temp_output_backups(backups)
    service._discard_temp_output_backups(backups)

    assert (tmp_path / "output.aux").read_text(encoding="utf-8") == "old"
    assert (tmp_path / "output.log").read_text(encoding="utf-8") == "old log"
    assert not (tmp_path / "output.fls").exists()
    assert not list(tmp_path.glob(".sigma-latex-backup-*"))


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_restores_existing_temp_files_when_latexmk_raises(tmp_path):
    service = LaTeXService()
    (tmp_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )
    (tmp_path / "output.aux").write_text("old", encoding="utf-8")

    async def fake_run_latexmk(compile_dir_arg, main_name, engine_arg):
        (compile_dir_arg / "output.aux").write_text("new", encoding="utf-8")
        raise RuntimeError("boom")

    service._run_latexmk = fake_run_latexmk

    with pytest.raises(RuntimeError, match="boom"):
        await service._compile_latexmk_project(tmp_path, "main.tex", "main.tex", "pdflatex")

    assert (tmp_path / "output.aux").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".sigma-latex-backup-*"))


def test_cleanup_failed_keep_outputs_removes_new_pdf_and_synctex(tmp_path):
    service = LaTeXService()
    existing = service._snapshot_keep_outputs(tmp_path)

    (tmp_path / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
    (tmp_path / "output.synctex.gz").write_bytes(b"sync")

    service._cleanup_failed_keep_outputs(tmp_path, existing)

    assert not (tmp_path / "output.pdf").exists()
    assert not (tmp_path / "output.synctex.gz").exists()


def test_get_pdf_path_allows_nested_output_pdf(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()

    assert service.get_pdf_path("x", "chapters/output.pdf") == source_dir / "output.pdf"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_get_compile_status_uses_nested_main_file(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await service.get_compile_status("x")

    assert result == {"has_pdf": True, "pdf_files": ["chapters/output.pdf"]}


def test_compile_rejects_unsafe_main_file_paths():
    service = LaTeXService()

    assert service._is_safe_filename("chapters/main.tex") is True
    assert service._is_safe_filename("../main.tex") is False
    assert service._is_safe_filename("chapters/../main.tex") is False
    assert service._is_safe_filename(".hidden/main.tex") is False
    assert service._is_safe_filename("chapters\\main.tex") is False


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_backward_parses_input_file_after_line_number(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "main.tex").write_text("", encoding="utf-8")
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"SyncTeX result begin\nOutput:output.pdf\nInput:/tmp/ignored.tex\nLine:1\nOutput:output.pdf\nInput:main.tex\nLine:23\nSyncTeX result end\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert kwargs["cwd"] == str(source_dir)
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await service.run_synctex(
            "x",
            type("SyncData", (), {"type": "backward", "page": 1, "x": 0, "y": 0})(),
        )

    assert result["file"] == "chapters/main.tex"
    assert result["line"] == 23


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_backward_parses_input_file_without_line_number(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "main.tex").write_text("", encoding="utf-8")
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"This is SyncTeX command line utility, version 1.5\nSyncTeX result begin\nOutput:output.pdf\nInput:main.tex\nLine:136\nColumn:-1\nSyncTeX result end\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await service.run_synctex(
            "x",
            type("SyncData", (), {"type": "backward", "page": 1, "x": 0, "y": 0})(),
        )

    assert result["file"] == "chapters/main.tex"
    assert result["line"] == 136


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_backward_parses_absolute_input_file(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "main.tex").write_text("", encoding="utf-8")
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            output = (
                "SyncTeX result begin\n"
                "Output:output.pdf\n"
                f"Input:{source_dir}/./main.tex\n"
                "Line:64\n"
                "Column:-1\n"
                "SyncTeX result end\n"
            )
            return output.encode("utf-8"), b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await service.run_synctex(
            "x",
            type("SyncData", (), {"type": "backward", "page": 1, "x": 0, "y": 0})(),
        )

    assert result["file"] == "chapters/main.tex"
    assert result["line"] == 64


def test_parse_synctex_output_keeps_values_with_colons():
    service = LaTeXService()
    records = service._parse_synctex_output(
        "SyncTeX result begin\n"
        "Output:output.pdf\n"
        "Input:this-file:has-a-weird-name.tex\n"
        "Line:17\n"
        "SyncTeX result end\n"
    )

    assert records == [{"Input": "this-file:has-a-weird-name.tex", "Line": "17"}]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_backward_ignores_generated_files(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"SyncTeX result begin\nOutput:output.pdf\nInput:output.bbl\nLine:3\nSyncTeX result end\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex"},
    ):
        with pytest.raises(latex_service_module.SyncTeXError, match="No source location found"):
            await service.run_synctex(
                "x",
                type("SyncData", (), {"type": "backward", "page": 1, "x": 0, "y": 0})(),
            )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_forward_uses_absolute_source_path(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    source_dir = project_path / "chapters"
    source_dir.mkdir(parents=True)
    (source_dir / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)
    commands = []

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"SyncTeX result begin\nOutput:output.pdf\nPage:1\nh:10\nv:20\nSyncTeX result end\n", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "chapters/main.tex"},
    ):
        result = await service.run_synctex(
            "x",
            type("SyncData", (), {
                "type": "forward",
                "file": "chapters/main.tex",
                "line": 7,
                "column": 3,
            })(),
        )

    assert result["success"] is True
    assert f"7:3:{source_dir / 'main.tex'}" in commands[0]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_rejects_unknown_type(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex"},
    ):
        with pytest.raises(latex_service_module.SyncTeXError, match="Unsupported SyncTeX type"):
            await service.run_synctex(
                "x",
                type("SyncData", (), {"type": "sideways", "page": 1, "x": 0, "y": 0})(),
            )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_synctex_reports_nonzero_exit(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "output.pdf").write_bytes(b"%PDF" + b"x" * 1200)

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return b"", b"bad synctex"

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex"},
    ):
        with pytest.raises(latex_service_module.SyncTeXError, match="bad synctex"):
            await service.run_synctex(
                "x",
                type("SyncData", (), {
                    "type": "backward",
                    "page": 1,
                    "x": 0,
                    "y": 0,
                })(),
            )


def test_sanitize_synctex_rewrites_relative_inputs_to_absolute(tmp_path):
    synctex_path = tmp_path / "output.synctex.gz"
    with gzip.open(synctex_path, "wb") as handle:
        handle.write(b"Input:1:main.tex\nInput:2:/already/absolute.tex\n")

    LaTeXService._sanitize_synctex(synctex_path, str(tmp_path))

    with gzip.open(synctex_path, "rb") as handle:
        content = handle.read().decode("utf-8")
    assert f"Input:1:{tmp_path}/main.tex" in content
    assert "Input:2:/already/absolute.tex" in content


def test_parse_diagnostics_preserves_file_context_with_nested_parentheses():
    service = LaTeXService()
    log = "\n".join([
        "(./main.tex (see docs)",
        "(./chapter.tex",
        "! Undefined control sequence.",
        "l.12 \\bad",
        ")",
        "! Missing $ inserted.",
        "l.3 $",
    ])

    diagnostics = service._parse_diagnostics(log, "main.tex")

    assert diagnostics[0]["file"] == "chapter.tex"
    assert diagnostics[0]["line"] == 12
    assert diagnostics[1]["file"] == "main.tex"
    assert diagnostics[1]["line"] == 3


def test_resolve_main_file_rejects_multiple_candidates(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    (project_path / "a").mkdir(parents=True)
    (project_path / "b").mkdir()
    (project_path / "a" / "main.tex").write_text("", encoding="utf-8")
    (project_path / "b" / "main.tex").write_text("", encoding="utf-8")

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]

        def get_project_path(self, project_id):
            return project_path

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    service = LaTeXService()

    with pytest.raises(latex_service_module.LaTeXCompilationError, match="Multiple files named main.tex"):
        service.resolve_main_file("x", "main.tex")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_run_latexmk_times_out_and_terminates_process(tmp_path, monkeypatch):
    service = LaTeXService()
    signaled_groups: list[tuple[int, int]] = []  # (pgid, signal)
    killed = {"value": False}

    class FakeProcess:
        returncode = None
        pid = 4242

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        async def wait(self):
            # Models a wedged process tree: ignores SIGTERM, only exits
            # once the group has been SIGKILLed.
            if not killed["value"]:
                await asyncio.sleep(60)
            self.returncode = -9
            return self.returncode

    def fake_getpgid(pid):
        return 9999

    def fake_killpg(pgid, sig):
        signaled_groups.append((pgid, sig))
        if sig == signal.SIGKILL:
            killed["value"] = True

    async def fake_create_subprocess_exec(*args, **kwargs):
        # ``start_new_session=True`` is now mandatory: the timeout teardown
        # relies on latexmk being its own process-group leader.
        assert kwargs.get("start_new_session") is True
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "LATEXMK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(latex_service_module, "LATEXMK_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(latex_service_module.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(latex_service_module.os, "killpg", fake_killpg)
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    success, stdout, stderr = await service._run_latexmk(tmp_path, "main.tex", "pdflatex")

    assert success is False
    assert stdout == ""
    assert "timed out" in stderr
    # SIGTERM must be sent to the whole group; when the group ignores it the
    # teardown escalates to SIGKILL.
    assert (9999, signal.SIGTERM) in signaled_groups
    assert (9999, signal.SIGKILL) in signaled_groups
    assert killed["value"] is True


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_run_latexmk_prepends_texlive_current_bin(tmp_path, monkeypatch):
    service = LaTeXService()
    captured_env = None
    texlive_root = tmp_path / "texlive"
    texlive_bin = texlive_root / "current" / "bin" / "aarch64-linux"
    texlive_bin.mkdir(parents=True)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"ok", b""

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal captured_env
        captured_env = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(latex_service_module, "TEXLIVE_ROOT", str(texlive_root))
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    success, stdout, stderr = await service._run_latexmk(tmp_path, "main.tex", "pdflatex")

    assert success is True
    assert stdout == "ok"
    assert stderr == ""
    assert captured_env["PATH"].split(":")[0] == str(texlive_bin)


def test_texlive_bin_dirs_prefers_current_platform(tmp_path, monkeypatch):
    service = LaTeXService()
    texlive_root = tmp_path / "texlive"
    current_bin = texlive_root / "current" / "bin"
    (current_bin / "aarch64-linux").mkdir(parents=True)
    (current_bin / "x86_64-linux").mkdir()

    monkeypatch.setattr(latex_service_module, "TEXLIVE_ROOT", str(texlive_root))
    monkeypatch.setattr(latex_service_module.platform, "machine", lambda: "x86_64")

    assert service._texlive_bin_dirs()[0] == str(current_bin / "x86_64-linux")


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_reports_missing_latexmk(tmp_path, monkeypatch):
    project_path = tmp_path / "project"
    project_path.mkdir()
    (project_path / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Hello\\end{document}",
        encoding="utf-8",
    )

    class FakeSettings:
        LATEX_ENGINES = ["pdflatex", "xelatex", "lualatex", "latex"]
        DEFAULT_LATEX_ENGINE = "pdflatex"

        def get_project_path(self, project_id):
            return project_path

    async def fake_create_subprocess_exec(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(latex_service_module, "settings", FakeSettings())
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    service = LaTeXService()

    with patch.object(
        project_service_module.project_service,
        "get_project",
        new_callable=AsyncMock,
        return_value={"id": "x", "main_file": "main.tex", "engine": "pdflatex"},
    ):
        result = await service.compile("x")

    assert result["success"] is False
    assert "latexmk command not found" in result["log"]


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_terminate_latexmk_kills_whole_process_group(tmp_path, monkeypatch):
    """Regression: a bad project can leave pdflatex in a loop that ignores
    SIGTERM. The teardown must SIGKILL the entire process group so the
    children cannot escape as orphans.

    The fake leader ignores SIGTERM (only exits on SIGKILL), modelling a
    wedged latexmk/child tree. We assert SIGKILL is sent to the group and
    that the leader is awaited afterward.
    """
    service = LaTeXService()
    signaled: list[tuple[int, int]] = []
    killed = {"value": False}
    wait_calls = 0

    class FakeProcess:
        pid = 4242
        returncode = None

        async def wait(self):
            nonlocal wait_calls
            wait_calls += 1
            if not killed["value"]:
                await asyncio.sleep(60)
            self.returncode = -9
            return self.returncode

    def fake_getpgid(pid):
        return 7777

    def fake_killpg(pgid, sig):
        signaled.append((pgid, sig))
        if sig == signal.SIGKILL:
            killed["value"] = True

    monkeypatch.setattr(latex_service_module, "LATEXMK_TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(latex_service_module.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(latex_service_module.os, "killpg", fake_killpg)

    process = FakeProcess()
    await service._terminate_latexmk(process)

    assert (7777, signal.SIGTERM) in signaled
    assert (7777, signal.SIGKILL) in signaled
    # SIGTERM phase waits, times out, then SIGKILL phase also waits on the
    # leader so asyncio's child watcher does not leak a zombie.
    assert wait_calls >= 2
    assert killed["value"] is True


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_terminate_latexmk_handles_already_exited_leader(monkeypatch):
    """If the leader has already exited (ProcessLookupError on getpgid),
    teardown must still await the leader without signaling."""
    service = LaTeXService()
    killpg_calls: list[tuple[int, int]] = []
    wait_calls = 0

    class FakeProcess:
        pid = 4242
        returncode = 0

        async def wait(self):
            nonlocal wait_calls
            wait_calls += 1
            return self.returncode

    def fake_getpgid(pid):
        raise ProcessLookupError

    def fake_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    monkeypatch.setattr(latex_service_module.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(latex_service_module.os, "killpg", fake_killpg)

    await service._terminate_latexmk(FakeProcess())

    assert killpg_calls == []
    assert wait_calls == 1


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_terminate_latexmk_survives_group_gone_race(monkeypatch):
    """``killpg`` may raise ProcessLookupError if the group exits between our
    getpgid and the signal. Teardown must not crash — it still has to await
    the leader so we don't leak a zombie."""
    service = LaTeXService()
    wait_calls = 0

    class FakeProcess:
        pid = 4242
        returncode = -15

        async def wait(self):
            nonlocal wait_calls
            wait_calls += 1
            return self.returncode

    def fake_getpgid(pid):
        return 7777

    def fake_killpg(pgid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(latex_service_module.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(latex_service_module.os, "killpg", fake_killpg)

    # No assertion on signals — we only assert teardown completes without
    # raising despite killpg racing.
    await service._terminate_latexmk(FakeProcess())
    assert wait_calls >= 1


# ---------------------------------------------------------------------------
# compile_project: preemption — a new compile cancels the previous one.
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_compile_tasks():
    """Clear the module-level compile orchestration state before and after
    each test so concurrent-compile tests don't leak state into each other."""
    latex_service_module._compile_tasks.clear()
    latex_service_module._compile_preempted.clear()
    latex_service_module._compile_orchestration_locks.clear()
    yield
    latex_service_module._compile_tasks.clear()
    latex_service_module._compile_preempted.clear()
    latex_service_module._compile_orchestration_locks.clear()


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_new_compile_cancels_previous(isolated_compile_tasks):
    """A new compile_project call cancels the previous in-flight compile.

    The previous caller sees CompileCancelledError (a clear, surfacable
    message); the new task's result is what the new caller sees.
    """
    service = LaTeXService()

    compile_started = asyncio.Event()
    compile_should_finish = asyncio.Event()
    call_log: list[str] = []

    async def fake_compile(project_id, main_file, engine):
        call_log.append(f"compile:{main_file}")
        compile_started.set()
        # Block until the test lets us finish, modeling a long compile.
        await compile_should_finish.wait()
        return {"success": True, "pdf_path": "output.pdf", "log": "", "diagnostics": []}

    async def fake_inner_noop(project_id, request_main_file, engine):
        """Bypass main-file resolution so we can drive fake_compile directly."""
        call_log.append(f"inner:{request_main_file}")
        return await fake_compile(project_id, request_main_file, engine)

    service._compile_project_inner = fake_inner_noop

    # Start the "previous" compile — it will block in fake_compile.
    previous_task = asyncio.create_task(service.compile_project("p1", "old.tex"))
    await compile_started.wait()

    # The in-flight task must be registered.
    assert "p1" in latex_service_module._compile_tasks
    registered_previous = latex_service_module._compile_tasks["p1"]
    assert not registered_previous.done()

    # Start the "new" compile. It must cancel the previous one, then run.
    new_task = asyncio.create_task(service.compile_project("p1", "new.tex"))

    # The previous caller sees CompileCancelledError, not a raw
    # CancelledError — that's what lets the unified exception handler turn
    # it into a clear message instead of "Internal server error".
    with pytest.raises(latex_service_module.CompileCancelledError):
        await previous_task

    # Let the new compile finish.
    compile_should_finish.set()
    new_result = await new_task

    assert new_result["success"] is True
    assert call_log == ["inner:old.tex", "compile:old.tex", "inner:new.tex", "compile:new.tex"]
    # Registration cleaned up once the new task finished.
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_previous_cancelled_propagates(isolated_compile_tasks):
    """When the previous compile is cancelled by a newer request, its caller
    sees CompileCancelledError (surfaced as a clear message, not 500)."""
    service = LaTeXService()

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_inner(project_id, request_main_file, engine):
        started.set()
        await release.wait()
        return {"success": True, "pdf_path": "output.pdf", "log": "", "diagnostics": []}

    service._compile_project_inner = fake_inner

    # Caller A: awaits compile_project directly (as the route does).
    caller_a = asyncio.create_task(service.compile_project("p1", "a.tex"))
    await started.wait()

    # Caller B cancels A's compile.
    caller_b = asyncio.create_task(service.compile_project("p1", "b.tex"))

    with pytest.raises(latex_service_module.CompileCancelledError):
        await caller_a

    release.set()
    result_b = await caller_b
    assert result_b["success"] is True
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_external_cancel_still_propagates(isolated_compile_tasks):
    """A cancel that comes from OUTSIDE the preemption path (client disconnect,
    shutdown) must NOT be converted to CompileCancelledError. The task is not
    in ``_compile_preempted`` because no newer request marked it, so the raw
    CancelledError must propagate so the caller (e.g. FastAPI client-disconnect
    handling) sees the original signal type."""
    service = LaTeXService()

    started = asyncio.Event()

    async def fake_inner(project_id, request_main_file, engine):
        started.set()
        await asyncio.sleep(60)  # long; will be cancelled externally
        return {"success": True, "pdf_path": "output.pdf", "log": "", "diagnostics": []}

    service._compile_project_inner = fake_inner

    caller = asyncio.create_task(service.compile_project("p1", "a.tex"))
    await started.wait()

    # External cancel: no newer request marked this task as preempted.
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_external_cancel_after_preemption_classifies_correctly(
    isolated_compile_tasks,
):
    """Regression: an external cancel (client disconnect) that happens to a
    compile whose OWN start had preempted an earlier compile must still be
    classified as external — NOT as preemption.

    With an epoch-counter design this was misclassified: B bumped the epoch
    when preempting A, so when B itself was later cancelled externally the
    epoch had "moved" and B was wrongly reported as preempted. The
    per-task ``_compile_preempted`` flag only marks the *victim* (A), not
    the *initiator* (B), so B's external cancel stays raw."""
    service = LaTeXService()

    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def inner_a(project_id, request_main_file, engine):
        a_started.set()
        await release_a.wait()
        return {"success": True, "pdf_path": "a.pdf", "log": "", "diagnostics": []}

    b_started = asyncio.Event()

    async def inner_b(project_id, request_main_file, engine):
        b_started.set()
        await asyncio.sleep(60)  # B will be cancelled externally
        return {"success": True, "pdf_path": "b.pdf", "log": "", "diagnostics": []}

    # A starts and blocks; B preempts A; then B is cancelled externally.
    service._compile_project_inner = inner_a
    task_a = asyncio.create_task(service.compile_project("p1", "a.tex"))
    await a_started.wait()

    service._compile_project_inner = inner_b
    task_b = asyncio.create_task(service.compile_project("p1", "b.tex"))
    await b_started.wait()

    # A was the victim of B's preemption -> CompileCancelledError.
    with pytest.raises(latex_service_module.CompileCancelledError):
        await task_a

    # B is externally cancelled (client disconnect). Despite having preempted
    # A earlier, B is NOT itself preempted -> raw CancelledError.
    task_b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task_b
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_no_previous_runs_normally(isolated_compile_tasks):
    """When no previous compile is running, compile_project just runs the
    new one — no cancel path involved."""
    service = LaTeXService()

    async def fake_inner(project_id, request_main_file, engine):
        return {"success": True, "pdf_path": "output.pdf", "log": "", "diagnostics": []}

    service._compile_project_inner = fake_inner

    result = await service.compile_project("p1", "main.tex")

    assert result["success"] is True
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_registers_and_clears_on_exception(isolated_compile_tasks):
    """If the inner compile raises (not CancelledError), the registration is
    still cleared so a later compile can start fresh."""
    service = LaTeXService()

    async def fake_inner(project_id, request_main_file, engine):
        raise RuntimeError("boom")

    service._compile_project_inner = fake_inner

    with pytest.raises(RuntimeError, match="boom"):
        await service.compile_project("p1", "main.tex")

    # Registration cleared even though inner raised.
    assert "p1" not in latex_service_module._compile_tasks


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_compile_project_does_not_overwrite_later_registration(isolated_compile_tasks):
    """When task A finishes after a later task B has already registered, A's
    finally must NOT clear B's registration. Guards against the
    "replace-then-stale-clear" race."""
    service = LaTeXService()

    a_started = asyncio.Event()
    release_a = asyncio.Event()

    async def inner_a(project_id, request_main_file, engine):
        a_started.set()
        try:
            await release_a.wait()
        finally:
            pass
        return {"success": True, "pdf_path": "a.pdf", "log": "", "diagnostics": []}

    b_started = asyncio.Event()
    release_b = asyncio.Event()

    async def inner_b(project_id, request_main_file, engine):
        b_started.set()
        await release_b.wait()
        return {"success": True, "pdf_path": "b.pdf", "log": "", "diagnostics": []}

    # Drive A: install inner_a, start compile_project for A, wait until A's
    # inner is running, then swap the implementation to inner_b before B
    # starts so B uses inner_b.
    service._compile_project_inner = inner_a
    task_a = asyncio.create_task(service.compile_project("p1", "a.tex"))
    await a_started.wait()

    service._compile_project_inner = inner_b
    task_b = asyncio.create_task(service.compile_project("p1", "b.tex"))

    # B cancels A's inner (preemption). A's outer compile_project converts
    # that to CompileCancelledError since B has already re-registered.
    with pytest.raises(latex_service_module.CompileCancelledError):
        await task_a

    # A's finally must NOT have wiped B's registration: by the time A's
    # outer finishes, B's inner is registered under "p1".
    await b_started.wait()
    assert latex_service_module._compile_tasks.get("p1") is not None

    release_b.set()
    result_b = await task_b
    assert result_b["pdf_path"] == "b.pdf"
    assert "p1" not in latex_service_module._compile_tasks
