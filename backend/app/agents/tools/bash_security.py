"""
Bash command security validation.

Detects command injection patterns, dangerous shell metacharacters,
wrapper stripping, and env var stripping.
"""

import re


# ---------------------------------------------------------------------------
# Command substitution patterns
# ---------------------------------------------------------------------------
COMMAND_SUBSTITUTION_PATTERNS = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "Zsh process substitution =()"),
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "Zsh equals expansion (=cmd)"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"\$\{"), "${} parameter substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "Zsh-style parameter expansion"),
    (re.compile(r"\(e:"), "Zsh-style glob qualifiers"),
    (re.compile(r"\(\+"), "Zsh glob qualifier with command execution"),
    (re.compile(r"\}\s*always\s*\{"), "Zsh always block (try/always construct)"),
    (re.compile(r"<#"), "PowerShell comment syntax"),
]

# Heredoc-in-substitution detection
_HEREDOC_IN_SUBSTITUTION = re.compile(r"\$\(.*<<")


# ---------------------------------------------------------------------------
# Wrapper stripping
# ---------------------------------------------------------------------------

# SECURITY: Use [ \t]+ not \s+ — \s matches \n/\r which are command separators.
# Matching across a newline would strip the wrapper from one line and leave a
# different command on the next line for bash to execute.

_SAFE_WRAPPER_PATTERNS = [
    # timeout with flags and duration
    re.compile(
        r"^timeout[ \t]+(?:(?:--(?:foreground|preserve-status|verbose)"
        r"|--(?:kill-after|signal)=[A-Za-z0-9_.+-]+"
        r"|--(?:kill-after|signal)[ \t]+[A-Za-z0-9_.+-]+"
        r"|-v|-[ks][ \t]+[A-Za-z0-9_.+-]+|-[ks][A-Za-z0-9_.+-]+"
        r")[ \t]+)*(?:--[ \t]+)?\d+(?:\.\d+)?[smhd]?[ \t]+"
    ),
    # time
    re.compile(r"^time[ \t]+(?:--[ \t]+)?"),
    # nice
    re.compile(r"^nice(?:[ \t]+-n[ \t]+-?\d+|[ \t]+-\d+)?[ \t]+(?:--[ \t]+)?"),
    # stdbuf
    re.compile(r"^stdbuf(?:[ \t]+-[ioe][LN0-9]+)+[ \t]+(?:--[ \t]+)?"),
    # nohup
    re.compile(r"^nohup[ \t]+(?:--[ \t]+)?"),
]


# ---------------------------------------------------------------------------
# Safe environment variables
# ---------------------------------------------------------------------------
# SECURITY: These must NEVER include PATH, LD_PRELOAD, LD_LIBRARY_PATH,
# DYLD_*, PYTHONPATH, NODE_PATH, CLASSPATH, RUBYLIB, GOFLAGS, RUSTFLAGS,
# NODE_OPTIONS, HOME, TMPDIR, SHELL, BASH_ENV (execution/library loading).

SAFE_ENV_VARS = frozenset({
    # Go
    "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GO111MODULE",
    # Rust
    "RUST_BACKTRACE", "RUST_LOG",
    # Node
    "NODE_ENV",
    # Python
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    # Pytest
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG",
    # API keys
    "ANTHROPIC_API_KEY",
    # Locale
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_TIME", "CHARSET",
    # Terminal
    "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "TZ",
    # Color config
    "LS_COLORS", "LSCOLORS", "GREP_COLOR", "GREP_COLORS", "GCC_COLORS",
    # Display
    "TIME_STYLE", "BLOCK_SIZE", "BLOCKSIZE",
})

# Env var assignment pattern (VAR=value, safe chars only)
_ENV_VAR_PATTERN = re.compile(r"^([A-Za-z_]\w*)=([A-Za-z0-9_./:-]+)[ \t]+")

# Comment line pattern
_COMMENT_LINE_PATTERN = re.compile(r"^\s*#")


def strip_comment_lines(command: str) -> str:
    """Strip full-line comments from a command."""
    lines = command.split("\n")
    non_comment = [l for l in lines if l.strip() and not _COMMENT_LINE_PATTERN.match(l)]
    return "\n".join(non_comment) if non_comment else command


def strip_safe_wrappers(command: str) -> str:
    """Strip safe wrapper commands and env vars from the beginning of a command.

    Phase 1: Strip leading safe env vars and comment lines.
    Phase 2: Strip wrapper commands (timeout, time, nice, nohup).

    Returns the stripped command.
    """
    stripped = command
    prev = ""

    # Phase 1: strip env vars and comments
    while stripped != prev:
        prev = stripped
        stripped = strip_comment_lines(stripped)
        m = _ENV_VAR_PATTERN.match(stripped)
        if m:
            var_name = m.group(1)
            if var_name in SAFE_ENV_VARS:
                stripped = stripped[m.end():]

    # Phase 2: strip wrapper commands
    prev = ""
    while stripped != prev:
        prev = stripped
        stripped = strip_comment_lines(stripped)
        for pattern in _SAFE_WRAPPER_PATTERNS:
            stripped = pattern.sub("", stripped)

    return stripped.strip()


def strip_all_leading_env_vars(command: str) -> str:
    """Strip ALL leading env var prefixes regardless of safety.

    Used for deny/ask rule matching — a denied command should stay denied
    regardless of env var prefixes.
    """
    # Broader value pattern that excludes shell injection characters
    broad_pattern = re.compile(
        r"^([A-Za-z_]\w*(?:\[[^\]]*\])?)\+?="
        r"(?:'[^'\n\r]*'|\"(?:\\.|[^\"$`\\\n\r])*\"|\\.|[^ \t\n\r$`;|&()<>\\\\'\"])*"
        r"[ \t]+"
    )

    stripped = command
    prev = ""
    while stripped != prev:
        prev = stripped
        stripped = strip_comment_lines(stripped)
        m = broad_pattern.match(stripped)
        if m:
            stripped = stripped[m.end():]

    return stripped.strip()


# ---------------------------------------------------------------------------
# Quote extraction
# ---------------------------------------------------------------------------

def _has_unescaped_char(content: str, char: str) -> bool:
    """Check if content contains an unescaped occurrence of a single character."""
    if len(char) != 1:
        raise ValueError("Only single characters supported")
    i = 0
    while i < len(content):
        if content[i] == "\\" and i + 1 < len(content):
            i += 2  # skip escape sequence
            continue
        if content[i] == char:
            return True
        i += 1
    return False


def _extract_unquoted(command: str) -> str:
    """Extract content with all quoting removed (fully unquoted)."""
    result = []
    in_single = False
    in_double = False
    escaped = False
    for ch in command:
        if escaped:
            escaped = False
            if not in_single:
                result.append(ch)
            continue
        if ch == "\\" and not in_single:
            escaped = True
            if not in_single and not in_double:
                result.append(ch)
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single:
            result.append(ch)
    return "".join(result)


def _strip_safe_redirections(content: str) -> str:
    """Strip safe redirections like > /dev/null and 2>&1."""
    content = re.sub(r"\s+2\s*>&\s*1(?=\s|$)", "", content)
    content = re.sub(r"[012]?\s*>\s*/dev/null(?=\s|$)", "", content)
    content = re.sub(r"\s*<\s*/dev/null(?=\s|$)", "", content)
    return content


# ---------------------------------------------------------------------------
# Security validators
# ---------------------------------------------------------------------------

def bash_command_is_safe(command: str) -> bool:
    """Run all security validators on a command.

    Returns True if the command passes all checks (safe / passthrough),
    False if any validator flags it as dangerous (ask).
    """
    if not command or not command.strip():
        return True  # Empty command is safe

    unquoted = _extract_unquoted(command)
    fully_unquoted = _strip_safe_redirections(_extract_unquoted(command))

    # 1. Check for incomplete commands
    trimmed = command.strip()
    if re.match(r"^\s*\t", command):
        return False  # Starts with tab (incomplete fragment)
    if trimmed.startswith("-"):
        return False  # Starts with flags
    if re.match(r"^\s*(&&|\|\||;|>>?|<)", command):
        return False  # Continuation line

    # 2. Check for command substitution patterns
    if _has_unescaped_char(unquoted, "`"):
        return False
    for pattern, _msg in COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted):
            return False

    # 3. Check for redirections
    if "<" in fully_unquoted:
        return False
    if ">" in fully_unquoted:
        return False

    # 4. Check for newlines that could separate commands
    pre_strip_unquoted = _extract_unquoted(command)
    if re.search(r"[\n\r]", pre_strip_unquoted):
        # Flag newline/CR followed by non-whitespace (except backslash continuation)
        if re.search(r"(?<![\s]\\)[\n\r]\s*\S", pre_strip_unquoted):
            return False

    # 5. Check for IFS injection
    if re.search(r"\$IFS|\$\{[^}]*IFS", command):
        return False

    # 6. Check for /proc environ access
    if re.search(r"/proc/.*?/environ", command):
        return False

    # 7. Check for dangerous variables in redirect/pipe context
    if re.search(r"[<>|]\s*\$[A-Za-z_]", fully_unquoted):
        return False
    if re.search(r"\$[A-Za-z_]\w*\s*[|<>]", fully_unquoted):
        return False

    # 8. Check for shell metacharacters in arguments
    if re.search(r"""(?:^|\s)["'][^"']*[;&][^"']*["'](?:\s|$)""", unquoted):
        return False

    # 9. Check for ANSI-C quoting / locale quoting (obfuscation)
    if re.search(r"\$'[^']*'", command):
        return False
    if re.search(r'\$"[^"]*"', command):
        return False

    # 10. Check for empty quotes before dash (bypass attempt)
    if re.search(r"""(?:^|\s)(?:''|"")+"""+r"""\s*-""", command):
        return False

    return True


# ---------------------------------------------------------------------------
# Compound command splitting
# ---------------------------------------------------------------------------

def split_compound_command(command: str) -> list[str]:
    """Split a compound command (with &&, ||, |, ;) into individual subcommands.

    Respects quoting — operators inside quotes are not treated as separators.
    """
    subcommands = []
    current: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0

    while i < len(command):
        ch = command[i]

        if escaped:
            escaped = False
            current.append(ch)
            i += 1
            continue

        if ch == "\\" and not in_single:
            escaped = True
            current.append(ch)
            i += 1
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue

        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue

        if in_single or in_double:
            current.append(ch)
            i += 1
            continue

        # Check for compound operators
        if ch == ";":
            sub = "".join(current).strip()
            if sub:
                subcommands.append(sub)
            current = []
            i += 1
            continue

        if ch == "&" and i + 1 < len(command) and command[i + 1] == "&":
            sub = "".join(current).strip()
            if sub:
                subcommands.append(sub)
            current = []
            i += 2
            continue

        if ch == "|" and i + 1 < len(command) and command[i + 1] == "|":
            sub = "".join(current).strip()
            if sub:
                subcommands.append(sub)
            current = []
            i += 2
            continue

        if ch == "|":
            sub = "".join(current).strip()
            if sub:
                subcommands.append(sub)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    sub = "".join(current).strip()
    if sub:
        subcommands.append(sub)

    return subcommands


# ---------------------------------------------------------------------------
# Redirect extraction and validation
# ---------------------------------------------------------------------------

def extract_redirect_paths(command: str) -> list[str]:
    """Extract output redirection target paths from a command.

    Returns list of file paths that would be written to.
    """
    paths = []
    unquoted = _extract_unquoted(command)

    # Match > or >> followed by a path
    for match in re.finditer(r">{1,2}\s*(\S+)", unquoted):
        path = match.group(1)
        if path and path != "/dev/null":
            paths.append(path)

    return paths


def is_subshell(command: str) -> bool:
    """Check if the command uses subshell syntax."""
    return bool(re.search(r"\$\(", command) or "`" in command)
