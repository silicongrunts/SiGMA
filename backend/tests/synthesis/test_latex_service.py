"""
Tests for latex_service.get_pdf_filename() (migrated from compile_service).
"""

import asyncio
import gzip
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
    terminated = False

    class FakeProcess:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def terminate(self):
            nonlocal terminated
            terminated = True

        def kill(self):
            pass

        async def wait(self):
            self.returncode = -15
            return self.returncode

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(latex_service_module, "LATEXMK_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        latex_service_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    success, stdout, stderr = await service._run_latexmk(tmp_path, "main.tex", "pdflatex")

    assert success is False
    assert stdout == ""
    assert "timed out" in stderr
    assert terminated is True


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
