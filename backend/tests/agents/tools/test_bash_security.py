"""
Security contract tests for the bash command gate.

The bash tool auto-approves only read-only commands and routes everything
else to the permission dialog. The decision flow lives in
``bash_permissions.check_bash_permission``, which combines the
``bash_security`` validators (injection detection, wrapper/env stripping,
compound splitting, redirect extraction) with the ``bash_readonly``
allowlist. These tests pin that boundary:

- injection / substitution constructs must never be auto-approved
- compound commands are judged per subcommand: one dangerous clause makes
  the whole command require approval
- a read-only command must not gain write powers via output redirection
- safe wrappers (timeout/time/nice/stdbuf/nohup) and safe env vars are
  stripped before classification, so wrapped allowlist commands stay
  read-only
- empty or malformed input must return a verdict, never raise
"""

import pytest

from app.agents.tools.bash_permissions import check_bash_permission
from app.agents.tools.bash_readonly import is_command_read_only
from app.agents.tools.bash_security import (
    bash_command_is_safe,
    extract_redirect_paths,
    is_subshell,
    split_compound_command,
    strip_all_leading_env_vars,
    strip_safe_wrappers,
)

pytestmark = pytest.mark.security


# ── injection & malformed fragments must reach the approval dialog ──

@pytest.mark.parametrize("command", [
    "echo $(cat /etc/passwd)",   # $() command substitution
    "echo `id`",                 # backtick substitution
    "echo ${HOME}",              # ${} parameter substitution
    "echo $'hello'",             # ANSI-C quoting obfuscation
    "echo $IFS",                 # IFS injection
    "cat /proc/1/environ",       # environ scraping
    "echo hi > $FILE",           # variable expansion into a redirect
    "ls\nrm -rf /",              # newline as command separator
    "\tls -la",                  # tab-indented fragment (incomplete command)
    "-la file",                  # flags-only fragment
    "&& ls",                     # continuation line
    "cat < /etc/shadow",         # input redirection
])
def test_injection_and_fragment_constructs_are_never_auto_approved(command):
    """Every flagged construct must fail the safety validator and land in
    the approval dialog, never in silent auto-approval."""
    assert bash_command_is_safe(command) is False
    result = check_bash_permission(command)
    assert result.approved is False
    assert result.content == command  # the dialog shows what would have run


def test_approval_content_carries_full_command():
    """When the classifier has no richer explanation, the approval dialog's
    content must carry the full command so the user reviews exactly what
    would run."""
    long_command = "rm -f " + " ".join(f"/tmp/artifact-{i}.log" for i in range(200))
    result = check_bash_permission(long_command)
    assert not result.approved
    assert result.content == long_command


@pytest.mark.parametrize("command,expected", [
    ("echo $(whoami)", True),
    ("echo `whoami`", True),
    ("git log --oneline", False),
    ("echo 'plain'", False),
])
def test_is_subshell_detects_substitution_syntax(command, expected):
    assert is_subshell(command) is expected


# ── compound commands are judged per subcommand ─────────────────────

@pytest.mark.parametrize("separator", ["&&", "||", ";", "|"])
def test_one_dangerous_clause_poisons_whole_compound(separator):
    """A single non-allowlisted clause must force approval of the entire
    command, regardless of how safe the other clauses are."""
    command = f"git status {separator} rm -rf /"
    result = check_bash_permission(command)
    assert result.approved is False
    assert "rm" in result.reason


def test_compound_of_readonly_commands_is_auto_approved():
    result = check_bash_permission("git status && git diff || git log -1")
    assert result.approved is True


def test_quoted_operator_is_not_a_separator():
    """The && inside single quotes is literal text: the command stays one
    read-only echo and must not be split into phantom subcommands."""
    assert split_compound_command("echo 'a && b' | wc") == ["echo 'a && b'", "wc"]
    result = check_bash_permission("echo 'a && b'")
    assert result.approved is True


@pytest.mark.parametrize("command,expected", [
    ("ls && rm -rf /", ["ls", "rm -rf /"]),
    ("a; b", ["a", "b"]),
    ("ls || true", ["ls", "true"]),
    ("git log | grep x | wc -l", ["git log", "grep x", "wc -l"]),
    ('echo "x;y"', ['echo "x;y"']),
    ("", []),
])
def test_split_compound_command(command, expected):
    assert split_compound_command(command) == expected


# ── redirection must not grant write powers to read-only commands ───

@pytest.mark.parametrize("command", [
    "git status > notes.txt",
    "git status >> notes.txt",
    "ls >> out.log",
])
def test_readonly_command_with_output_redirect_needs_approval(command):
    """Appending output redirection to an allowlisted command must never
    stay auto-approved — the redirect writes files."""
    assert bash_command_is_safe(command) is False
    assert check_bash_permission(command).approved is False


def test_devnull_redirect_stays_read_only():
    """Discarding output (the only harmless redirect) is still auto-approved."""
    result = check_bash_permission("git log 2>/dev/null")
    assert result.approved is True


@pytest.mark.parametrize("command,expected", [
    ("git status > notes.txt", ["notes.txt"]),
    ("git status >> notes.txt", ["notes.txt"]),
    ("git log > /dev/null", []),                 # /dev/null is not a write target
    ("echo hi", []),
    ("sort < in.txt > out.txt", ["out.txt"]),    # input redirect is not collected
])
def test_extract_redirect_paths(command, expected):
    assert extract_redirect_paths(command) == expected


# ── safe wrapper / env-var stripping ────────────────────────────────

@pytest.mark.parametrize("command,stripped", [
    ("timeout 10 git status", "git status"),
    ("timeout --foreground 10 git status", "git status"),
    ("time git status", "git status"),
    ("nice -n 5 git status", "git status"),
    ("nice git status", "git status"),
    ("stdbuf -oL git status", "git status"),
    ("nohup git status", "git status"),
    ("GOOS=linux CGO_ENABLED=0 go build ./...", "go build ./..."),
])
def test_strip_safe_wrappers(command, stripped):
    assert strip_safe_wrappers(command) == stripped


@pytest.mark.parametrize("wrapped", [
    "timeout 30 git status",
    "time git status",
    "nice git status",
    "stdbuf -oL git status",
    "nohup git status",
])
def test_wrapped_allowlist_command_stays_read_only(wrapped):
    """Wrapping an allowlisted command in a harmless wrapper must not push
    it out of the read-only classification."""
    assert is_command_read_only(strip_safe_wrappers(wrapped)) is True
    assert check_bash_permission(wrapped).approved is True


def test_wrapper_stripping_does_not_cross_newline():
    """The wrapper patterns must use space/tab only: a newline is a command
    separator, and stripping across it would hide a second command from
    classification."""
    command = "timeout 10\nrm -rf /"
    assert strip_safe_wrappers(command) == command
    assert check_bash_permission(command).approved is False


def test_unsafe_env_var_prefix_is_not_stripped():
    """LD_PRELOAD-style prefixes are outside SAFE_ENV_VARS: the read-only
    classification must see them (and refuse), while deny-rule matching
    strips them separately so a deny stays a deny."""
    command = "LD_PRELOAD=/evil/so ls"
    assert strip_safe_wrappers(command) == command
    assert check_bash_permission(command).approved is False
    assert strip_all_leading_env_vars(command) == "ls"


# ── empty / malformed input must not crash the classifier ───────────

@pytest.mark.parametrize("command", [
    "", "   ", "\t", "\n", ">>>", "&&", "||", ";", "|", ">", "<",
    "$((", "${", "`", "'\"", "echo \"unterminated", "echo 'unterminated",
    "\\", "\x00", "===",
])
def test_malformed_input_returns_verdict_never_raises(command):
    """Every classifier entry point must produce a verdict for garbage
    input instead of raising into the agent loop."""
    bash_command_is_safe(command)
    split_compound_command(command)
    strip_safe_wrappers(command)
    extract_redirect_paths(command)
    is_subshell(command)
    is_command_read_only(command)
    check_bash_permission(command)


def test_empty_command_is_treated_as_harmless():
    assert bash_command_is_safe("") is True
    assert bash_command_is_safe("   ") is True
    assert split_compound_command("") == []
    assert extract_redirect_paths("") == []
    assert is_subshell("") is False
    assert check_bash_permission("   ").approved is True
