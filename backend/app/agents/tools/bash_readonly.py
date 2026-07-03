"""
Bash read-only command validation.

Only commands in these whitelists are auto-approved as read-only.
Everything else requires user permission.
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Flag argument types
# ---------------------------------------------------------------------------
# 'none'    — flag takes no argument (e.g. -v, --verbose)
# 'number'  — flag takes a numeric argument (e.g. -m 50)
# 'string'  — flag takes a string argument (e.g. -e "pattern")
# 'char'    — flag takes a single character (e.g. -d '\n')
# Specific string values like '{}' or 'EOF' indicate the exact expected value.
FLAG_ARG_NONE = "none"
FLAG_ARG_NUMBER = "number"
FLAG_ARG_STRING = "string"
FLAG_ARG_CHAR = "char"


# ---------------------------------------------------------------------------
# Git read-only commands — safe flags per subcommand
# ---------------------------------------------------------------------------

_GIT_REF_SELECTION_FLAGS = {
    "--sha": FLAG_ARG_STRING, "--resolve": FLAG_ARG_NONE,
    "--abbrev": FLAG_ARG_NUMBER, "--no-abbrev": FLAG_ARG_NONE,
    "--short": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
    "--remotes": FLAG_ARG_NONE, "--tags": FLAG_ARG_NONE,
    "--heads": FLAG_ARG_NONE, "--local": FLAG_ARG_NONE,
    "--merged": FLAG_ARG_STRING, "--no-merged": FLAG_ARG_STRING,
    "--contains": FLAG_ARG_STRING, "--no-contains": FLAG_ARG_STRING,
    "--exclude": FLAG_ARG_STRING,
}

_GIT_DATE_FILTER_FLAGS = {
    "--since": FLAG_ARG_STRING, "--after": FLAG_ARG_STRING,
    "--until": FLAG_ARG_STRING, "--before": FLAG_ARG_STRING,
}

_GIT_LOG_DISPLAY_FLAGS = {
    "--oneline": FLAG_ARG_NONE, "--graph": FLAG_ARG_NONE,
    "--decorate": FLAG_ARG_STRING, "--no-decorate": FLAG_ARG_NONE,
    "--source": FLAG_ARG_NONE, "--walk-reflogs": FLAG_ARG_NONE,
    "--full-history": FLAG_ARG_NONE, "--simplify-merges": FLAG_ARG_NONE,
    "--ancestry-path": FLAG_ARG_NONE, "--first-parent": FLAG_ARG_NONE,
    "--no-walk": FLAG_ARG_NONE, "--do-walk": FLAG_ARG_NONE,
    "-q": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
    "-p": FLAG_ARG_NONE, "-u": FLAG_ARG_NONE, "--patch": FLAG_ARG_NONE,
    "-s": FLAG_ARG_NONE, "--no-patch": FLAG_ARG_NONE,
    "--raw": FLAG_ARG_NONE, "--patch-with-raw": FLAG_ARG_NONE,
    "--patch-with-stat": FLAG_ARG_NONE,
}

_GIT_COUNT_FLAGS = {
    "--count": FLAG_ARG_NONE,
}

_GIT_STAT_FLAGS = {
    "--stat": FLAG_ARG_STRING, "--numstat": FLAG_ARG_NONE,
    "--shortstat": FLAG_ARG_NONE, "--summary": FLAG_ARG_NONE,
    "--dirstat": FLAG_ARG_STRING, "--cumulative": FLAG_ARG_NONE,
}

_GIT_COLOR_FLAGS = {
    "--color": FLAG_ARG_STRING, "--no-color": FLAG_ARG_NONE,
}

_GIT_PATCH_FLAGS = {
    "-w": FLAG_ARG_NONE, "--ignore-all-space": FLAG_ARG_NONE,
    "-b": FLAG_ARG_NONE, "--ignore-space-change": FLAG_ARG_NONE,
    "--ignore-space-at-eol": FLAG_ARG_NONE,
    "--ignore-cr-at-eol": FLAG_ARG_NONE,
    "--indent-heuristic": FLAG_ARG_NONE,
    "--no-indent-heuristic": FLAG_ARG_NONE,
    "--textconv": FLAG_ARG_NONE, "--no-textconv": FLAG_ARG_NONE,
    "-R": FLAG_ARG_NONE,
}

_GIT_AUTHOR_FILTER_FLAGS = {
    "--author": FLAG_ARG_STRING, "--committer": FLAG_ARG_STRING,
    "--grep": FLAG_ARG_STRING, "--grep-reflog": FLAG_ARG_STRING,
    "--all-match": FLAG_ARG_NONE, "--invert-grep": FLAG_ARG_NONE,
    "--basic-regexp": FLAG_ARG_NONE,
    "-E": FLAG_ARG_NONE, "--extended-regexp": FLAG_ARG_NONE,
    "-P": FLAG_ARG_NONE, "--perl-regexp": FLAG_ARG_NONE,
    "-F": FLAG_ARG_NONE, "--fixed-strings": FLAG_ARG_NONE,
    "-i": FLAG_ARG_NONE, "--regexp-ignore-case": FLAG_ARG_NONE,
}

GIT_READ_ONLY_COMMANDS = {
    "git diff": {
        "safeFlags": {
            "--cached": FLAG_ARG_NONE, "--staged": FLAG_ARG_NONE,
            "--name-only": FLAG_ARG_NONE, "--name-status": FLAG_ARG_NONE,
            "--no-index": FLAG_ARG_NONE, "--exit-code": FLAG_ARG_NONE,
            "--quiet": FLAG_ARG_NONE,
            "--no-ext-diff": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE, "-a": FLAG_ARG_NONE,
            "--no-textconv": FLAG_ARG_NONE, "--check": FLAG_ARG_NONE,
            "--ws-error-highlight": FLAG_ARG_STRING,
            "-U": FLAG_ARG_NUMBER, "--unified": FLAG_ARG_NUMBER,
            "--anchored": FLAG_ARG_STRING,
            "--diff-algorithm": FLAG_ARG_STRING,
            "--diff-filter": FLAG_ARG_STRING,
            "--follow": FLAG_ARG_NONE, "--no-follow": FLAG_ARG_NONE,
            "--relative": FLAG_ARG_STRING, "--no-relative": FLAG_ARG_NONE,
            "-S": FLAG_ARG_STRING, "-G": FLAG_ARG_STRING,
            "--find-object": FLAG_ARG_STRING,
            "--pickaxe-all": FLAG_ARG_NONE, "--pickaxe-regex": FLAG_ARG_NONE,
            "-O": FLAG_ARG_STRING,
            "--skip-to": FLAG_ARG_STRING, "--rotate-to": FLAG_ARG_STRING,
            "-M": FLAG_ARG_NONE, "--find-renames": FLAG_ARG_NONE,
            "--no-find-renames": FLAG_ARG_NONE,
            "-D": FLAG_ARG_STRING,
            "-C": FLAG_ARG_NONE, "--find-copies": FLAG_ARG_NONE,
            "--find-copies-harder": FLAG_ARG_NONE,
            "--irreversible-delete": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NUMBER, "--diff-merges": FLAG_ARG_STRING,
            "--no-diff-merges": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NONE, "--first-parent": FLAG_ARG_NONE,
            "--reverse": FLAG_ARG_NONE,
            "--reflog": FLAG_ARG_NONE, "--walk-reflogs": FLAG_ARG_NONE,
            "--merge-base": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE,
            "--abbrev": FLAG_ARG_NUMBER, "--no-abbrev": FLAG_ARG_NONE,
            "--src-prefix": FLAG_ARG_STRING, "--dst-prefix": FLAG_ARG_STRING,
            "--line-prefix": FLAG_ARG_STRING,
            "--no-prefix": FLAG_ARG_NONE,
            "--inter-hunk-context": FLAG_ARG_NUMBER,
            "--break-rewrites": FLAG_ARG_STRING,
            "--no-break-rewrites": FLAG_ARG_NONE,
            "--column": FLAG_ARG_STRING, "--no-column": FLAG_ARG_NONE,
            "--bisect": FLAG_ARG_NONE,
            **_GIT_STAT_FLAGS, **_GIT_COLOR_FLAGS, **_GIT_PATCH_FLAGS,
        },
    },
    "git log": {
        "safeFlags": {
            "-n": FLAG_ARG_NUMBER, "--max-count": FLAG_ARG_NUMBER,
            "--skip": FLAG_ARG_NUMBER,
            "-L": FLAG_ARG_STRING,
            "--diff-merges": FLAG_ARG_STRING, "--no-diff-merges": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--cc": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NONE,
            "--reverse": FLAG_ARG_NONE,
            "--walk-reflogs": FLAG_ARG_NONE,
            "--format": FLAG_ARG_STRING, "--pretty": FLAG_ARG_STRING,
            "--notes": FLAG_ARG_STRING, "--no-notes": FLAG_ARG_NONE,
            "--show-notes": FLAG_ARG_STRING,
            "--expand-tabs": FLAG_ARG_STRING, "--no-expand-tabs": FLAG_ARG_NONE,
            "--show-linear-break": FLAG_ARG_STRING,
            "--show-signature": FLAG_ARG_NONE,
            "--no-show-signature": FLAG_ARG_NONE,
            "--mailmap": FLAG_ARG_NONE, "--no-mailmap": FLAG_ARG_NONE,
            "--log-size": FLAG_ARG_NONE,
            **_GIT_REF_SELECTION_FLAGS, **_GIT_DATE_FILTER_FLAGS,
            **_GIT_LOG_DISPLAY_FLAGS, **_GIT_COUNT_FLAGS,
            **_GIT_STAT_FLAGS, **_GIT_COLOR_FLAGS, **_GIT_PATCH_FLAGS,
            **_GIT_AUTHOR_FILTER_FLAGS,
        },
    },
    "git show": {
        "safeFlags": {
            "--format": FLAG_ARG_STRING, "--pretty": FLAG_ARG_STRING,
            "--abbrev": FLAG_ARG_NUMBER, "--no-abbrev": FLAG_ARG_NONE,
            "--name-only": FLAG_ARG_NONE, "--name-status": FLAG_ARG_NONE,
            "--notes": FLAG_ARG_STRING, "--no-notes": FLAG_ARG_NONE,
            "--show-signature": FLAG_ARG_NONE,
            "--quiet": FLAG_ARG_NONE,
            "--walk-reflogs": FLAG_ARG_NONE,
            "--source": FLAG_ARG_NONE,
            "--exclude": FLAG_ARG_STRING,
            **_GIT_STAT_FLAGS, **_GIT_COLOR_FLAGS, **_GIT_PATCH_FLAGS,
        },
    },
    "git shortlog": {
        "safeFlags": {
            "-n": FLAG_ARG_NONE, "--numbered": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--summary": FLAG_ARG_NONE,
            "-e": FLAG_ARG_NONE, "--email": FLAG_ARG_NONE,
            "-w": FLAG_ARG_STRING,
            **_GIT_AUTHOR_FILTER_FLAGS,
        },
    },
    "git reflog": {
        "safeFlags": {
            "show": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NUMBER, "--max-count": FLAG_ARG_NUMBER,
            "--format": FLAG_ARG_STRING, "--pretty": FLAG_ARG_STRING,
            **_GIT_DATE_FILTER_FLAGS, **_GIT_AUTHOR_FILTER_FLAGS,
        },
    },
    "git stash list": {
        "safeFlags": {
            "--format": FLAG_ARG_STRING, "--pretty": FLAG_ARG_STRING,
        },
    },
    "git ls-remote": {
        "safeFlags": {
            "--heads": FLAG_ARG_NONE, "-h": FLAG_ARG_NONE,
            "--tags": FLAG_ARG_NONE, "-t": FLAG_ARG_NONE,
            "--refs": FLAG_ARG_NONE,
            "--quiet": FLAG_ARG_NONE, "-q": FLAG_ARG_NONE,
            "--upload-pack": FLAG_ARG_NONE,  # blocked by callback
            "--sort": FLAG_ARG_STRING,
            "--exit-code": FLAG_ARG_NONE,
            "--get-url": FLAG_ARG_NONE,
            "--symref": FLAG_ARG_NONE,
        },
    },
    "git status": {
        "safeFlags": {
            "-s": FLAG_ARG_NONE, "--short": FLAG_ARG_NONE,
            "-b": FLAG_ARG_NONE, "--branch": FLAG_ARG_NONE,
            "--show-stash": FLAG_ARG_NONE,
            "--porcelain": FLAG_ARG_NONE,
            "--long": FLAG_ARG_NONE,
            "-u": FLAG_ARG_STRING, "--untracked-files": FLAG_ARG_STRING,
            "--ignore-submodules": FLAG_ARG_STRING,
            "--ignored": FLAG_ARG_STRING,
            "-z": FLAG_ARG_NONE, "--null": FLAG_ARG_NONE,
            "--no-renames": FLAG_ARG_NONE,
            "--find-renames": FLAG_ARG_STRING,
            "--column": FLAG_ARG_STRING, "--no-column": FLAG_ARG_NONE,
            **_GIT_COLOR_FLAGS,
        },
    },
    "git blame": {
        "safeFlags": {
            "-L": FLAG_ARG_STRING,
            "-l": FLAG_ARG_NONE, "--long": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE, "--time": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE,
            "-e": FLAG_ARG_NONE, "--show-email": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE,
            "-p": FLAG_ARG_NONE, "--porcelain": FLAG_ARG_NONE,
            "--line-porcelain": FLAG_ARG_NONE,
            "-M": FLAG_ARG_NUMBER,
            "-C": FLAG_ARG_NUMBER,
            "--root": FLAG_ARG_NONE,
            "--show-name": FLAG_ARG_NONE,
            "--show-number": FLAG_ARG_NONE,
            "--abbrev": FLAG_ARG_NUMBER,
            "--contents": FLAG_ARG_STRING,
            "--reverse": FLAG_ARG_NONE,
            **_GIT_DATE_FILTER_FLAGS,
        },
    },
    "git ls-files": {
        "safeFlags": {
            "-c": FLAG_ARG_NONE, "--cached": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NONE, "--deleted": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NONE, "--modified": FLAG_ARG_NONE,
            "-o": FLAG_ARG_NONE, "--others": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "--ignored": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--stage": FLAG_ARG_NONE,
            "-u": FLAG_ARG_NONE, "--unmerged": FLAG_ARG_NONE,
            "-k": FLAG_ARG_NONE,
            "--directory": FLAG_ARG_NONE,
            "--no-empty-directory": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE,
            "-f": FLAG_ARG_NONE,
            "--full-name": FLAG_ARG_NONE,
            "--recurse-submodules": FLAG_ARG_NONE,
            "-x": FLAG_ARG_STRING, "--exclude": FLAG_ARG_STRING,
            "-X": FLAG_ARG_STRING, "--exclude-from": FLAG_ARG_STRING,
            "--exclude-per-directory": FLAG_ARG_STRING,
            "--exclude-standard": FLAG_ARG_NONE,
            "-e": FLAG_ARG_STRING,
            "--error-unmatch": FLAG_ARG_NONE,
            "--abbrev": FLAG_ARG_NUMBER,
            "--debug": FLAG_ARG_NONE,
            "--deduplicate": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE,
        },
    },
    "git config": {
        "safeFlags": {
            "--get": FLAG_ARG_STRING,
            "--get-all": FLAG_ARG_STRING,
            "--get-regexp": FLAG_ARG_STRING,
            "--list": FLAG_ARG_NONE, "-l": FLAG_ARG_NONE,
            "--show-origin": FLAG_ARG_NONE,
            "--show-scope": FLAG_ARG_NONE,
            "--system": FLAG_ARG_NONE,
            "--global": FLAG_ARG_NONE,
            "--local": FLAG_ARG_NONE,
            "--file": FLAG_ARG_STRING, "-f": FLAG_ARG_STRING,
            "--blob": FLAG_ARG_STRING,
            "--int": FLAG_ARG_NONE,
            "--bool": FLAG_ARG_NONE,
            "--bool-or-int": FLAG_ARG_NONE,
            "--bool-or-str": FLAG_ARG_NONE,
            "--path": FLAG_ARG_NONE,
            "--expiry-date": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE, "--null": FLAG_ARG_NONE,
            "--name-only": FLAG_ARG_NONE,
            "--includes": FLAG_ARG_NONE, "--no-includes": FLAG_ARG_NONE,
            "--show-name": FLAG_ARG_NONE,
            "-e": FLAG_ARG_NONE,
        },
    },
    "git remote show": {
        "safeFlags": {
            "-n": FLAG_ARG_NONE,
            "--verbose": FLAG_ARG_NONE, "-v": FLAG_ARG_NONE,
        },
    },
    "git remote": {
        "safeFlags": {
            "-v": FLAG_ARG_NONE, "--verbose": FLAG_ARG_NONE,
            "show": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE,
        },
    },
    "git merge-base": {
        "safeFlags": {
            "-a": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
            "--is-ancestor": FLAG_ARG_NONE,
            "--independent": FLAG_ARG_NONE,
            "--fork-point": FLAG_ARG_NONE,
            "--octopus": FLAG_ARG_NONE,
        },
    },
    "git rev-parse": {
        "safeFlags": {
            "--verify": FLAG_ARG_NONE,
            "--quiet": FLAG_ARG_NONE, "-q": FLAG_ARG_NONE,
            "--sq": FLAG_ARG_NONE,
            "--short": FLAG_ARG_NUMBER,
            "--long": FLAG_ARG_NONE,
            "--abbrev-ref": FLAG_ARG_NONE,
            "--symbolic": FLAG_ARG_NONE,
            "--symbolic-full-name": FLAG_ARG_NONE,
            "--all": FLAG_ARG_NONE,
            "--branches": FLAG_ARG_STRING,
            "--tags": FLAG_ARG_STRING,
            "--remotes": FLAG_ARG_STRING,
            "--glob": FLAG_ARG_STRING,
            "--local": FLAG_ARG_NONE,
            "--show-toplevel": FLAG_ARG_NONE,
            "--show-prefix": FLAG_ARG_NONE,
            "--show-cdup": FLAG_ARG_NONE,
            "--git-dir": FLAG_ARG_NONE,
            "--absolute-git-dir": FLAG_ARG_NONE,
            "--show-superproject-working-tree": FLAG_ARG_NONE,
            "--shared-index-path": FLAG_ARG_NONE,
            "--since": FLAG_ARG_STRING,
            "--after": FLAG_ARG_STRING,
            "--until": FLAG_ARG_STRING,
            "--before": FLAG_ARG_STRING,
            "--resolve": FLAG_ARG_NONE,
        },
    },
    "git rev-list": {
        "safeFlags": {
            "-n": FLAG_ARG_NUMBER, "--max-count": FLAG_ARG_NUMBER,
            "--skip": FLAG_ARG_NUMBER,
            "--count": FLAG_ARG_NONE,
            "--objects": FLAG_ARG_NONE,
            "--objects-edge": FLAG_ARG_NONE,
            "--no-objects": FLAG_ARG_NONE,
            "--all": FLAG_ARG_NONE,
            "--remotes": FLAG_ARG_STRING,
            "--branches": FLAG_ARG_STRING,
            "--tags": FLAG_ARG_STRING,
            "--glob": FLAG_ARG_STRING,
            "--stdin": FLAG_ARG_NONE,
            "--quiet": FLAG_ARG_NONE,
            "--abbrev-commit": FLAG_ARG_NONE,
            "--abbrev": FLAG_ARG_NUMBER,
            "--no-walk": FLAG_ARG_STRING,
            "--do-walk": FLAG_ARG_NONE,
            "--timestamp": FLAG_ARG_NONE,
            "--left-right": FLAG_ARG_NONE,
            **_GIT_DATE_FILTER_FLAGS,
        },
    },
    "git describe": {
        "safeFlags": {
            "--all": FLAG_ARG_NONE,
            "--tags": FLAG_ARG_NONE,
            "--contains": FLAG_ARG_NONE,
            "--abbrev": FLAG_ARG_NUMBER,
            "--candidates": FLAG_ARG_NUMBER,
            "--exact-match": FLAG_ARG_NONE,
            "--debug": FLAG_ARG_NONE,
            "--long": FLAG_ARG_NONE,
            "--match": FLAG_ARG_STRING,
            "--exclude": FLAG_ARG_STRING,
            "--always": FLAG_ARG_NONE,
            "--dirty": FLAG_ARG_STRING,
            "--broken": FLAG_ARG_STRING,
            "--first-parent": FLAG_ARG_NONE,
        },
    },
    "git cat-file": {
        "safeFlags": {
            "-t": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE,
            "-e": FLAG_ARG_NONE,
            "-p": FLAG_ARG_NONE,
            "-b": FLAG_ARG_NONE,
            "--batch": FLAG_ARG_STRING,
            "--batch-check": FLAG_ARG_STRING,
            "--batch-all-objects": FLAG_ARG_NONE,
            "--allow-unknown-type": FLAG_ARG_NONE,
            "--buffer": FLAG_ARG_NONE,
            "--unordered": FLAG_ARG_NONE,
            "--dir-info": FLAG_ARG_NONE,
        },
    },
    "git for-each-ref": {
        "safeFlags": {
            "--format": FLAG_ARG_STRING,
            "--sort": FLAG_ARG_STRING,
            "--count": FLAG_ARG_NUMBER,
            "--shell": FLAG_ARG_NONE,
            "--perl": FLAG_ARG_NONE,
            "--python": FLAG_ARG_NONE,
            "--tcl": FLAG_ARG_NONE,
            "--json": FLAG_ARG_NONE,
            "--merged": FLAG_ARG_STRING,
            "--no-merged": FLAG_ARG_STRING,
            "--contains": FLAG_ARG_STRING,
            "--no-contains": FLAG_ARG_STRING,
            "--ignore-case": FLAG_ARG_NONE,
            "--no-ignored-case": FLAG_ARG_NONE,
            "--points-at": FLAG_ARG_STRING,
            **_GIT_COLOR_FLAGS,
        },
    },
    "git grep": {
        "safeFlags": {
            "-n": FLAG_ARG_NONE, "--line-number": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "-H": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NONE, "--files-with-matches": FLAG_ARG_NONE,
            "-L": FLAG_ARG_NONE, "--files-without-match": FLAG_ARG_NONE,
            "-e": FLAG_ARG_STRING,
            "-f": FLAG_ARG_STRING,
            "-i": FLAG_ARG_NONE, "--ignore-case": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--invert-match": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE,
            "-x": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--count": FLAG_ARG_NONE,
            "-o": FLAG_ARG_NONE, "--only-matching": FLAG_ARG_NONE,
            "-p": FLAG_ARG_NONE, "--show-function": FLAG_ARG_NONE,
            "-W": FLAG_ARG_NONE, "--function-context": FLAG_ARG_NONE,
            "-E": FLAG_ARG_NONE, "--extended-regexp": FLAG_ARG_NONE,
            "-G": FLAG_ARG_NONE, "--basic-regexp": FLAG_ARG_NONE,
            "-P": FLAG_ARG_NONE, "--perl-regexp": FLAG_ARG_NONE,
            "-F": FLAG_ARG_NONE, "--fixed-strings": FLAG_ARG_NONE,
            "-I": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE, "--null": FLAG_ARG_NONE,
            "--heading": FLAG_ARG_NONE,
            "--break": FLAG_ARG_NONE,
            "--show-name": FLAG_ARG_NONE,
            "--no-index": FLAG_ARG_NONE,
            "--untracked": FLAG_ARG_NONE,
            "--no-exclude-standard": FLAG_ARG_NONE,
            "--recurse-submodules": FLAG_ARG_NONE,
            "--parent-basename": FLAG_ARG_STRING,
            "-m": FLAG_ARG_NUMBER, "--max-depth": FLAG_ARG_NUMBER,
            "-a": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE,
            "--all-match": FLAG_ARG_NONE,
            "--and": FLAG_ARG_NONE, "--or": FLAG_ARG_NONE, "--not": FLAG_ARG_NONE,
            **_GIT_COLOR_FLAGS,
        },
    },
    "git stash show": {
        "safeFlags": {
            "--stat": FLAG_ARG_NONE,
            "--name-only": FLAG_ARG_NONE,
            "--name-status": FLAG_ARG_NONE,
            "--patch": FLAG_ARG_NONE, "-p": FLAG_ARG_NONE,
            "--include-untracked": FLAG_ARG_NONE,
            "--only-untracked": FLAG_ARG_NONE,
            **_GIT_COLOR_FLAGS,
        },
    },
    "git worktree list": {
        "safeFlags": {
            "--porcelain": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--verbose": FLAG_ARG_NONE,
        },
    },
    "git tag": {
        "safeFlags": {
            "-l": FLAG_ARG_STRING, "--list": FLAG_ARG_STRING,
            "-n": FLAG_ARG_NUMBER,
            "--contains": FLAG_ARG_STRING,
            "--no-contains": FLAG_ARG_STRING,
            "--merged": FLAG_ARG_STRING,
            "--no-merged": FLAG_ARG_STRING,
            "--sort": FLAG_ARG_STRING,
            "--format": FLAG_ARG_STRING,
            "--color": FLAG_ARG_STRING,
            **_GIT_REF_SELECTION_FLAGS,
        },
    },
    "git branch": {
        "safeFlags": {
            "-l": FLAG_ARG_STRING, "--list": FLAG_ARG_STRING,
            "-a": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
            "-r": FLAG_ARG_NONE, "--remotes": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "-vv": FLAG_ARG_NONE,
            "--verbose": FLAG_ARG_NONE,
            "-q": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "--show-current": FLAG_ARG_NONE,
            "--contains": FLAG_ARG_STRING,
            "--no-contains": FLAG_ARG_STRING,
            "--merged": FLAG_ARG_STRING,
            "--no-merged": FLAG_ARG_STRING,
            "--sort": FLAG_ARG_STRING,
            "--format": FLAG_ARG_STRING,
            "--abbrev": FLAG_ARG_NUMBER, "--no-abbrev": FLAG_ARG_NONE,
            **_GIT_COLOR_FLAGS,
        },
    },
}


# ---------------------------------------------------------------------------
# Docker read-only commands
# ---------------------------------------------------------------------------
DOCKER_READ_ONLY_COMMANDS = {
    "docker logs": {
        "safeFlags": {
            "-f": FLAG_ARG_NONE, "--follow": FLAG_ARG_NONE,
            "--since": FLAG_ARG_STRING,
            "--tail": FLAG_ARG_STRING,
            "-t": FLAG_ARG_NONE, "--timestamps": FLAG_ARG_NONE,
            "--until": FLAG_ARG_STRING,
            "-n": FLAG_ARG_STRING, "--last": FLAG_ARG_STRING,
        },
    },
    "docker inspect": {
        "safeFlags": {
            "-f": FLAG_ARG_STRING, "--format": FLAG_ARG_STRING,
            "-s": FLAG_ARG_NONE, "--size": FLAG_ARG_NONE,
            "--type": FLAG_ARG_STRING,
        },
    },
}


# ---------------------------------------------------------------------------
# Ripgrep read-only commands
# ---------------------------------------------------------------------------
RIPGREP_READ_ONLY_COMMANDS = {
    "rg": {
        "safeFlags": {
            "-e": FLAG_ARG_STRING, "--regexp": FLAG_ARG_STRING,
            "-f": FLAG_ARG_STRING, "--file": FLAG_ARG_STRING,
            "-F": FLAG_ARG_NONE, "--fixed-strings": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "--ignore-case": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--invert-match": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--word-regexp": FLAG_ARG_NONE,
            "-x": FLAG_ARG_NONE, "--line-regexp": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--count": FLAG_ARG_NONE,
            "--count-matches": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NONE, "--files-with-matches": FLAG_ARG_NONE,
            "-L": FLAG_ARG_NONE, "--follow": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NUMBER, "--max-count": FLAG_ARG_NUMBER,
            "-o": FLAG_ARG_NONE, "--only-matching": FLAG_ARG_NONE,
            "-q": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--case-sensitive": FLAG_ARG_NONE,
            "-S": FLAG_ARG_NONE, "--smart-case": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE, "--line-number": FLAG_ARG_NONE,
            "-N": FLAG_ARG_NONE, "--no-line-number": FLAG_ARG_NONE,
            "-H": FLAG_ARG_NONE, "--with-filename": FLAG_ARG_NONE,
            "-I": FLAG_ARG_NONE, "--no-filename": FLAG_ARG_NONE,
            "-r": FLAG_ARG_STRING, "--replace": FLAG_ARG_STRING,
            "-t": FLAG_ARG_STRING, "--type": FLAG_ARG_STRING,
            "-T": FLAG_ARG_STRING, "--type-not": FLAG_ARG_STRING,
            "--type-add": FLAG_ARG_STRING,
            "--type-clear": FLAG_ARG_STRING,
            "-g": FLAG_ARG_STRING, "--glob": FLAG_ARG_STRING,
            "--iglob": FLAG_ARG_STRING,
            "--ignore-file": FLAG_ARG_STRING,
            "--ignore-file-case-insensitive": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NUMBER, "--max-depth": FLAG_ARG_NUMBER,
            "--max-filesize": FLAG_ARG_STRING,
            "--max-columns": FLAG_ARG_NUMBER,
            "--max-columns-preview": FLAG_ARG_NONE,
            "--mmap": FLAG_ARG_NONE, "--no-mmap": FLAG_ARG_NONE,
            "--no-messages": FLAG_ARG_NONE,
            "--no-ignore": FLAG_ARG_NONE,
            "--no-ignore-global": FLAG_ARG_NONE,
            "--no-ignore-parent": FLAG_ARG_NONE,
            "--no-ignore-vcs": FLAG_ARG_NONE,
            "--no-ignore-dot": FLAG_ARG_NONE,
            "--no-ignore-exclude": FLAG_ARG_NONE,
            "--no-ignore-files": FLAG_ARG_NONE,
            "--no-require-git": FLAG_ARG_NONE,
            "--binary": FLAG_ARG_NONE,
            "-U": FLAG_ARG_NONE, "--multiline": FLAG_ARG_NONE,
            "--multiline-dotall": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE, "--search-zip": FLAG_ARG_NONE,
            "-0": FLAG_ARG_NONE, "--null": FLAG_ARG_NONE,
            "-A": FLAG_ARG_NUMBER, "--after-context": FLAG_ARG_NUMBER,
            "-B": FLAG_ARG_NUMBER, "--before-context": FLAG_ARG_NUMBER,
            "-C": FLAG_ARG_NUMBER, "--context": FLAG_ARG_NUMBER,
            "--column": FLAG_ARG_NONE,
            "--context-separator": FLAG_ARG_STRING,
            "--no-context-separator": FLAG_ARG_NONE,
            "--debug": FLAG_ARG_NONE,
            "--files": FLAG_ARG_NONE,
            "-j": FLAG_ARG_NUMBER, "--threads": FLAG_ARG_NUMBER,
            "--block-buffered": FLAG_ARG_NONE,
            "--line-buffered": FLAG_ARG_NONE,
            "--sort": FLAG_ARG_STRING,
            "--sortr": FLAG_ARG_STRING,
            "--path-separator": FLAG_ARG_STRING,
            "--trim": FLAG_ARG_NONE,
            "--crlf": FLAG_ARG_NONE, "--no-crlf": FLAG_ARG_NONE,
            "--encoding": FLAG_ARG_STRING,
            "--hyperlink-format": FLAG_ARG_STRING,
            "--json": FLAG_ARG_NONE,
            "--colors": FLAG_ARG_STRING,
            "--unicode": FLAG_ARG_NONE, "--no-unicode": FLAG_ARG_NONE,
            "-E": FLAG_ARG_STRING, "--encoding": FLAG_ARG_STRING,
            "--pcre2": FLAG_ARG_NONE, "--no-pcre2": FLAG_ARG_NONE,
            "--pcre2-version": FLAG_ARG_NONE,
            "-P": FLAG_ARG_NONE, "--perl-regexp": FLAG_ARG_NONE,
            "--no-pcre2-unicode": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
}


# ---------------------------------------------------------------------------
# Pyright read-only commands
# ---------------------------------------------------------------------------
PYRIGHT_READ_ONLY_COMMANDS = {
    "pyright": {
        "safeFlags": {
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
            "--verbose": FLAG_ARG_NONE,
            "--lib": FLAG_ARG_NONE,
            "--outputjson": FLAG_ARG_NONE,
            "-p": FLAG_ARG_STRING, "--project": FLAG_ARG_STRING,
            "-w": FLAG_ARG_NONE, "--watch": FLAG_ARG_NONE,
            "--stats": FLAG_ARG_NONE,
            "--verifytypes": FLAG_ARG_STRING,
            "--ignoreexternal": FLAG_ARG_NONE,
            "--pythonplatform": FLAG_ARG_STRING,
            "--pythonversion": FLAG_ARG_STRING,
        },
    },
}


# ---------------------------------------------------------------------------
# External read-only commands (simple regex-based)
# ---------------------------------------------------------------------------
EXTERNAL_READONLY_COMMANDS = ["docker ps", "docker images"]


# ---------------------------------------------------------------------------
# Command allowlist — per-command safe flags (readOnlyValidation.ts)
# ---------------------------------------------------------------------------

# Shared fd/fdfind safe flags
_FD_SAFE_FLAGS = {
    "-h": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE,
    "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
    "-H": FLAG_ARG_NONE, "--hidden": FLAG_ARG_NONE,
    "-I": FLAG_ARG_NONE, "--no-ignore": FLAG_ARG_NONE,
    "--no-ignore-vcs": FLAG_ARG_NONE, "--no-ignore-parent": FLAG_ARG_NONE,
    "-s": FLAG_ARG_NONE, "--case-sensitive": FLAG_ARG_NONE,
    "-i": FLAG_ARG_NONE, "--ignore-case": FLAG_ARG_NONE,
    "-g": FLAG_ARG_NONE, "--glob": FLAG_ARG_NONE,
    "--regex": FLAG_ARG_NONE,
    "-F": FLAG_ARG_NONE, "--fixed-strings": FLAG_ARG_NONE,
    "-a": FLAG_ARG_NONE, "--absolute-path": FLAG_ARG_NONE,
    "-L": FLAG_ARG_NONE, "--follow": FLAG_ARG_NONE,
    "-p": FLAG_ARG_NONE, "--full-path": FLAG_ARG_NONE,
    "-0": FLAG_ARG_NONE, "--print0": FLAG_ARG_NONE,
    "-d": FLAG_ARG_NUMBER, "--max-depth": FLAG_ARG_NUMBER,
    "--min-depth": FLAG_ARG_NUMBER, "--exact-depth": FLAG_ARG_NUMBER,
    "-t": FLAG_ARG_STRING, "--type": FLAG_ARG_STRING,
    "-e": FLAG_ARG_STRING, "--extension": FLAG_ARG_STRING,
    "-S": FLAG_ARG_STRING, "--size": FLAG_ARG_STRING,
    "--changed-within": FLAG_ARG_STRING, "--changed-before": FLAG_ARG_STRING,
    "-o": FLAG_ARG_STRING, "--owner": FLAG_ARG_STRING,
    "-E": FLAG_ARG_STRING, "--exclude": FLAG_ARG_STRING,
    "--ignore-file": FLAG_ARG_STRING,
    "-c": FLAG_ARG_STRING, "--color": FLAG_ARG_STRING,
    "-j": FLAG_ARG_NUMBER, "--threads": FLAG_ARG_NUMBER,
    "--max-buffer-time": FLAG_ARG_STRING,
    "--max-results": FLAG_ARG_NUMBER,
    "-1": FLAG_ARG_NONE, "-q": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
    "--show-errors": FLAG_ARG_NONE, "--strip-cwd-prefix": FLAG_ARG_NONE,
    "--one-file-system": FLAG_ARG_NONE,
    "--prune": FLAG_ARG_NONE,
    "--search-path": FLAG_ARG_STRING, "--base-directory": FLAG_ARG_STRING,
    "--path-separator": FLAG_ARG_STRING,
    "--batch-size": FLAG_ARG_NUMBER,
    "--no-require-git": FLAG_ARG_NONE,
    "--hyperlink": FLAG_ARG_STRING,
    "--and": FLAG_ARG_STRING,
    "--format": FLAG_ARG_STRING,
}

COMMAND_ALLOWLIST: dict[str, dict] = {
    "xargs": {
        "safeFlags": {
            "-I": "{}", "-n": FLAG_ARG_NUMBER, "-P": FLAG_ARG_NUMBER,
            "-L": FLAG_ARG_NUMBER, "-E": "EOF",
            "-0": FLAG_ARG_NONE, "-t": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE,
            "-x": FLAG_ARG_NONE, "-d": FLAG_ARG_CHAR,
        },
    },
    **GIT_READ_ONLY_COMMANDS,
    "file": {
        "safeFlags": {
            "--brief": FLAG_ARG_NONE, "-b": FLAG_ARG_NONE,
            "--mime": FLAG_ARG_NONE, "-i": FLAG_ARG_NONE,
            "--mime-type": FLAG_ARG_NONE, "--mime-encoding": FLAG_ARG_NONE,
            "--apple": FLAG_ARG_NONE,
            "--check-encoding": FLAG_ARG_NONE, "-c": FLAG_ARG_NONE,
            "--exclude": FLAG_ARG_STRING, "--exclude-quiet": FLAG_ARG_STRING,
            "--print0": FLAG_ARG_NONE, "-0": FLAG_ARG_NONE,
            "-f": FLAG_ARG_STRING, "-F": FLAG_ARG_STRING,
            "--separator": FLAG_ARG_STRING,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE, "-v": FLAG_ARG_NONE,
            "--no-dereference": FLAG_ARG_NONE, "-h": FLAG_ARG_NONE,
            "--dereference": FLAG_ARG_NONE, "-L": FLAG_ARG_NONE,
            "--magic-file": FLAG_ARG_STRING, "-m": FLAG_ARG_STRING,
            "--keep-going": FLAG_ARG_NONE, "-k": FLAG_ARG_NONE,
            "--list": FLAG_ARG_NONE, "-l": FLAG_ARG_NONE,
            "--no-buffer": FLAG_ARG_NONE, "-n": FLAG_ARG_NONE,
            "--preserve-date": FLAG_ARG_NONE, "-p": FLAG_ARG_NONE,
            "--raw": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--special-files": FLAG_ARG_NONE,
            "--uncompress": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE,
        },
    },
    "sed": {
        "safeFlags": {
            "--expression": FLAG_ARG_STRING, "-e": FLAG_ARG_STRING,
            "--quiet": FLAG_ARG_NONE, "--silent": FLAG_ARG_NONE, "-n": FLAG_ARG_NONE,
            "--regexp-extended": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE,
            "--posix": FLAG_ARG_NONE, "-E": FLAG_ARG_NONE,
            "--line-length": FLAG_ARG_NUMBER, "-l": FLAG_ARG_NUMBER,
            "--zero-terminated": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE,
            "--separate": FLAG_ARG_NONE, "-s": FLAG_ARG_NONE,
            "--unbuffered": FLAG_ARG_NONE, "-u": FLAG_ARG_NONE,
            "--debug": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "sort": {
        "safeFlags": {
            "--ignore-leading-blanks": FLAG_ARG_NONE, "-b": FLAG_ARG_NONE,
            "--dictionary-order": FLAG_ARG_NONE, "-d": FLAG_ARG_NONE,
            "--ignore-case": FLAG_ARG_NONE, "-f": FLAG_ARG_NONE,
            "--general-numeric-sort": FLAG_ARG_NONE, "-g": FLAG_ARG_NONE,
            "--human-numeric-sort": FLAG_ARG_NONE, "-h": FLAG_ARG_NONE,
            "--ignore-nonprinting": FLAG_ARG_NONE, "-i": FLAG_ARG_NONE,
            "--month-sort": FLAG_ARG_NONE, "-M": FLAG_ARG_NONE,
            "--numeric-sort": FLAG_ARG_NONE, "-n": FLAG_ARG_NONE,
            "--random-sort": FLAG_ARG_NONE, "-R": FLAG_ARG_NONE,
            "--reverse": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE,
            "--sort": FLAG_ARG_STRING,
            "--stable": FLAG_ARG_NONE, "-s": FLAG_ARG_NONE,
            "--unique": FLAG_ARG_NONE, "-u": FLAG_ARG_NONE,
            "--version-sort": FLAG_ARG_NONE, "-V": FLAG_ARG_NONE,
            "--zero-terminated": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE,
            "--key": FLAG_ARG_STRING, "-k": FLAG_ARG_STRING,
            "--field-separator": FLAG_ARG_STRING, "-t": FLAG_ARG_STRING,
            "--check": FLAG_ARG_NONE, "-c": FLAG_ARG_NONE,
            "--check-char-order": FLAG_ARG_NONE, "-C": FLAG_ARG_NONE,
            "--merge": FLAG_ARG_NONE, "-m": FLAG_ARG_NONE,
            "--buffer-size": FLAG_ARG_STRING, "-S": FLAG_ARG_STRING,
            "--parallel": FLAG_ARG_NUMBER,
            "--batch-size": FLAG_ARG_NUMBER,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "man": {
        "safeFlags": {
            "-a": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NONE, "-f": FLAG_ARG_NONE, "--whatis": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "-k": FLAG_ARG_NONE, "--apropos": FLAG_ARG_NONE,
            "-l": FLAG_ARG_STRING, "-w": FLAG_ARG_NONE,
            "-S": FLAG_ARG_STRING, "-s": FLAG_ARG_STRING,
        },
    },
    "help": {
        "safeFlags": {
            "-d": FLAG_ARG_NONE, "-m": FLAG_ARG_NONE, "-s": FLAG_ARG_NONE,
        },
    },
    "netstat": {
        "safeFlags": {
            "-a": FLAG_ARG_NONE, "-L": FLAG_ARG_NONE, "-l": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE, "-f": FLAG_ARG_STRING,
            "-g": FLAG_ARG_NONE, "-i": FLAG_ARG_NONE, "-I": FLAG_ARG_STRING,
            "-s": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE, "-m": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE,
        },
    },
    "ps": {
        "safeFlags": {
            "-e": FLAG_ARG_NONE, "-A": FLAG_ARG_NONE, "-a": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NONE, "-N": FLAG_ARG_NONE, "--deselect": FLAG_ARG_NONE,
            "-f": FLAG_ARG_NONE, "-F": FLAG_ARG_NONE, "-l": FLAG_ARG_NONE,
            "-j": FLAG_ARG_NONE, "-y": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "-ww": FLAG_ARG_NONE,
            "--width": FLAG_ARG_NUMBER,
            "-c": FLAG_ARG_NONE, "-H": FLAG_ARG_NONE,
            "--forest": FLAG_ARG_NONE, "--headers": FLAG_ARG_NONE,
            "--no-headers": FLAG_ARG_NONE,
            "-n": FLAG_ARG_STRING, "--sort": FLAG_ARG_STRING,
            "-L": FLAG_ARG_NONE, "-T": FLAG_ARG_NONE, "-m": FLAG_ARG_NONE,
            "-C": FLAG_ARG_STRING, "-G": FLAG_ARG_STRING,
            "-g": FLAG_ARG_STRING, "-p": FLAG_ARG_STRING, "--pid": FLAG_ARG_STRING,
            "-q": FLAG_ARG_STRING, "--quick-pid": FLAG_ARG_STRING,
            "-s": FLAG_ARG_STRING, "--sid": FLAG_ARG_STRING,
            "-t": FLAG_ARG_STRING, "--tty": FLAG_ARG_STRING,
            "-U": FLAG_ARG_STRING, "-u": FLAG_ARG_STRING, "--user": FLAG_ARG_STRING,
            "--help": FLAG_ARG_NONE, "--info": FLAG_ARG_NONE,
            "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "base64": {
        "safeFlags": {
            "-d": FLAG_ARG_NONE, "-D": FLAG_ARG_NONE, "--decode": FLAG_ARG_NONE,
            "-b": FLAG_ARG_NUMBER, "--break": FLAG_ARG_NUMBER,
            "-w": FLAG_ARG_NUMBER, "--wrap": FLAG_ARG_NUMBER,
            "-i": FLAG_ARG_STRING, "--input": FLAG_ARG_STRING,
            "--ignore-garbage": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "grep": {
        "safeFlags": {
            "-e": FLAG_ARG_STRING, "--regexp": FLAG_ARG_STRING,
            "-f": FLAG_ARG_STRING, "--file": FLAG_ARG_STRING,
            "-F": FLAG_ARG_NONE, "--fixed-strings": FLAG_ARG_NONE,
            "-G": FLAG_ARG_NONE, "--basic-regexp": FLAG_ARG_NONE,
            "-E": FLAG_ARG_NONE, "--extended-regexp": FLAG_ARG_NONE,
            "-P": FLAG_ARG_NONE, "--perl-regexp": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "--ignore-case": FLAG_ARG_NONE,
            "--no-ignore-case": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--invert-match": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--word-regexp": FLAG_ARG_NONE,
            "-x": FLAG_ARG_NONE, "--line-regexp": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--count": FLAG_ARG_NONE,
            "--color": FLAG_ARG_STRING, "--colour": FLAG_ARG_STRING,
            "-L": FLAG_ARG_NONE, "--files-without-match": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NONE, "--files-with-matches": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NUMBER, "--max-count": FLAG_ARG_NUMBER,
            "-o": FLAG_ARG_NONE, "--only-matching": FLAG_ARG_NONE,
            "-q": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "--silent": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--no-messages": FLAG_ARG_NONE,
            "-b": FLAG_ARG_NONE, "--byte-offset": FLAG_ARG_NONE,
            "-H": FLAG_ARG_NONE, "--with-filename": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "--no-filename": FLAG_ARG_NONE,
            "--label": FLAG_ARG_STRING,
            "-n": FLAG_ARG_NONE, "--line-number": FLAG_ARG_NONE,
            "-T": FLAG_ARG_NONE, "--initial-tab": FLAG_ARG_NONE,
            "-u": FLAG_ARG_NONE, "--unix-byte-offsets": FLAG_ARG_NONE,
            "-Z": FLAG_ARG_NONE, "--null": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE, "--null-data": FLAG_ARG_NONE,
            "-A": FLAG_ARG_NUMBER, "--after-context": FLAG_ARG_NUMBER,
            "-B": FLAG_ARG_NUMBER, "--before-context": FLAG_ARG_NUMBER,
            "-C": FLAG_ARG_NUMBER, "--context": FLAG_ARG_NUMBER,
            "--group-separator": FLAG_ARG_STRING,
            "--no-group-separator": FLAG_ARG_NONE,
            "-a": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE,
            "--binary-files": FLAG_ARG_STRING,
            "-D": FLAG_ARG_STRING, "--devices": FLAG_ARG_STRING,
            "-d": FLAG_ARG_STRING, "--directories": FLAG_ARG_STRING,
            "--exclude": FLAG_ARG_STRING, "--exclude-from": FLAG_ARG_STRING,
            "--exclude-dir": FLAG_ARG_STRING, "--include": FLAG_ARG_STRING,
            "-r": FLAG_ARG_NONE, "--recursive": FLAG_ARG_NONE,
            "-R": FLAG_ARG_NONE, "--dereference-recursive": FLAG_ARG_NONE,
            "--line-buffered": FLAG_ARG_NONE,
            "-U": FLAG_ARG_NONE, "--binary": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    **RIPGREP_READ_ONLY_COMMANDS,
    # Checksum commands
    "sha256sum": {
        "safeFlags": {
            "-b": FLAG_ARG_NONE, "--binary": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--check": FLAG_ARG_NONE,
            "--ignore-missing": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "--status": FLAG_ARG_NONE, "--strict": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--warn": FLAG_ARG_NONE,
            "--tag": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE, "--zero": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "sha1sum": {
        "safeFlags": {
            "-b": FLAG_ARG_NONE, "--binary": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--check": FLAG_ARG_NONE,
            "--ignore-missing": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "--status": FLAG_ARG_NONE, "--strict": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--warn": FLAG_ARG_NONE,
            "--tag": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE, "--zero": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "md5sum": {
        "safeFlags": {
            "-b": FLAG_ARG_NONE, "--binary": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE, "--text": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--check": FLAG_ARG_NONE,
            "--ignore-missing": FLAG_ARG_NONE, "--quiet": FLAG_ARG_NONE,
            "--status": FLAG_ARG_NONE, "--strict": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--warn": FLAG_ARG_NONE,
            "--tag": FLAG_ARG_NONE, "-z": FLAG_ARG_NONE, "--zero": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "tree": {
        "safeFlags": {
            "-a": FLAG_ARG_NONE, "-d": FLAG_ARG_NONE, "-l": FLAG_ARG_NONE,
            "-f": FLAG_ARG_NONE, "-x": FLAG_ARG_NONE,
            "-L": FLAG_ARG_NUMBER,
            "-P": FLAG_ARG_STRING, "-I": FLAG_ARG_STRING,
            "--gitignore": FLAG_ARG_NONE, "--gitfile": FLAG_ARG_STRING,
            "--ignore-case": FLAG_ARG_NONE, "--matchdirs": FLAG_ARG_NONE,
            "--metafirst": FLAG_ARG_NONE, "--prune": FLAG_ARG_NONE,
            "--info": FLAG_ARG_NONE, "--infofile": FLAG_ARG_STRING,
            "--noreport": FLAG_ARG_NONE, "--charset": FLAG_ARG_STRING,
            "--filelimit": FLAG_ARG_NUMBER,
            "-q": FLAG_ARG_NONE, "-N": FLAG_ARG_NONE, "-Q": FLAG_ARG_NONE,
            "-p": FLAG_ARG_NONE, "-u": FLAG_ARG_NONE, "-g": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "-h": FLAG_ARG_NONE,
            "--si": FLAG_ARG_NONE, "--du": FLAG_ARG_NONE,
            "-D": FLAG_ARG_NONE, "--timefmt": FLAG_ARG_STRING,
            "-F": FLAG_ARG_NONE, "--inodes": FLAG_ARG_NONE, "--device": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "-t": FLAG_ARG_NONE, "-c": FLAG_ARG_NONE,
            "-U": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE,
            "--dirsfirst": FLAG_ARG_NONE, "--filesfirst": FLAG_ARG_NONE,
            "--sort": FLAG_ARG_STRING,
            "-i": FLAG_ARG_NONE, "-A": FLAG_ARG_NONE, "-S": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE, "-C": FLAG_ARG_NONE,
            "-X": FLAG_ARG_NONE, "-J": FLAG_ARG_NONE,
            "-H": FLAG_ARG_STRING, "--nolinks": FLAG_ARG_NONE,
            "--hintro": FLAG_ARG_STRING, "--houtro": FLAG_ARG_STRING,
            "-T": FLAG_ARG_STRING,
            "--hyperlink": FLAG_ARG_NONE, "--scheme": FLAG_ARG_STRING,
            "--authority": FLAG_ARG_STRING,
            "--fromfile": FLAG_ARG_NONE, "--fromtabfile": FLAG_ARG_NONE,
            "--fflinks": FLAG_ARG_NONE,
            "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "date": {
        "safeFlags": {
            "-d": FLAG_ARG_STRING, "--date": FLAG_ARG_STRING,
            "-r": FLAG_ARG_STRING, "--reference": FLAG_ARG_STRING,
            "-u": FLAG_ARG_NONE, "--utc": FLAG_ARG_NONE, "--universal": FLAG_ARG_NONE,
            "-I": FLAG_ARG_NONE, "--iso-8601": FLAG_ARG_STRING,
            "-R": FLAG_ARG_NONE, "--rfc-email": FLAG_ARG_NONE,
            "--rfc-3339": FLAG_ARG_STRING,
            "--debug": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "hostname": {
        "safeFlags": {
            "-f": FLAG_ARG_NONE, "--fqdn": FLAG_ARG_NONE, "--long": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--short": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "--ip-address": FLAG_ARG_NONE,
            "-I": FLAG_ARG_NONE, "--all-ip-addresses": FLAG_ARG_NONE,
            "-a": FLAG_ARG_NONE, "--alias": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NONE, "--domain": FLAG_ARG_NONE,
            "-A": FLAG_ARG_NONE, "--all-fqdns": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--verbose": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE,
            "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "info": {
        "safeFlags": {
            "-f": FLAG_ARG_STRING, "--file": FLAG_ARG_STRING,
            "-d": FLAG_ARG_STRING, "--directory": FLAG_ARG_STRING,
            "-n": FLAG_ARG_STRING, "--node": FLAG_ARG_STRING,
            "-a": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
            "-k": FLAG_ARG_STRING, "--apropos": FLAG_ARG_STRING,
            "-w": FLAG_ARG_NONE, "--where": FLAG_ARG_NONE,
            "--location": FLAG_ARG_NONE, "--show-options": FLAG_ARG_NONE,
            "--vi-keys": FLAG_ARG_NONE, "--subnodes": FLAG_ARG_NONE,
            "-h": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE,
            "--usage": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "lsof": {
        "safeFlags": {
            "-?": FLAG_ARG_NONE, "-h": FLAG_ARG_NONE, "-v": FLAG_ARG_NONE,
            "-a": FLAG_ARG_NONE, "-b": FLAG_ARG_NONE, "-C": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NONE, "-n": FLAG_ARG_NONE, "-N": FLAG_ARG_NONE,
            "-O": FLAG_ARG_NONE, "-P": FLAG_ARG_NONE, "-Q": FLAG_ARG_NONE,
            "-R": FLAG_ARG_NONE, "-t": FLAG_ARG_NONE, "-U": FLAG_ARG_NONE,
            "-V": FLAG_ARG_NONE, "-X": FLAG_ARG_NONE, "-H": FLAG_ARG_NONE,
            "-E": FLAG_ARG_NONE, "-F": FLAG_ARG_NONE, "-g": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "-K": FLAG_ARG_NONE, "-L": FLAG_ARG_NONE,
            "-o": FLAG_ARG_NONE, "-r": FLAG_ARG_NONE, "-s": FLAG_ARG_NONE,
            "-S": FLAG_ARG_NONE, "-T": FLAG_ARG_NONE, "-x": FLAG_ARG_NONE,
            "-A": FLAG_ARG_STRING, "-c": FLAG_ARG_STRING,
            "-d": FLAG_ARG_STRING, "-e": FLAG_ARG_STRING,
            "-k": FLAG_ARG_STRING, "-p": FLAG_ARG_STRING, "-u": FLAG_ARG_STRING,
        },
    },
    "pgrep": {
        "safeFlags": {
            "-d": FLAG_ARG_STRING, "--delimiter": FLAG_ARG_STRING,
            "-l": FLAG_ARG_NONE, "--list-name": FLAG_ARG_NONE,
            "-a": FLAG_ARG_NONE, "--list-full": FLAG_ARG_NONE,
            "-v": FLAG_ARG_NONE, "--inverse": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--lightweight": FLAG_ARG_NONE,
            "-c": FLAG_ARG_NONE, "--count": FLAG_ARG_NONE,
            "-f": FLAG_ARG_NONE, "--full": FLAG_ARG_NONE,
            "-g": FLAG_ARG_STRING, "--pgroup": FLAG_ARG_STRING,
            "-G": FLAG_ARG_STRING, "--group": FLAG_ARG_STRING,
            "-i": FLAG_ARG_NONE, "--ignore-case": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE, "--newest": FLAG_ARG_NONE,
            "-o": FLAG_ARG_NONE, "--oldest": FLAG_ARG_NONE,
            "-O": FLAG_ARG_STRING, "--older": FLAG_ARG_STRING,
            "-P": FLAG_ARG_STRING, "--parent": FLAG_ARG_STRING,
            "-s": FLAG_ARG_STRING, "--session": FLAG_ARG_STRING,
            "-t": FLAG_ARG_STRING, "--terminal": FLAG_ARG_STRING,
            "-u": FLAG_ARG_STRING, "--euid": FLAG_ARG_STRING,
            "-U": FLAG_ARG_STRING, "--uid": FLAG_ARG_STRING,
            "-x": FLAG_ARG_NONE, "--exact": FLAG_ARG_NONE,
            "-F": FLAG_ARG_STRING, "--pidfile": FLAG_ARG_STRING,
            "-L": FLAG_ARG_NONE, "--logpidfile": FLAG_ARG_NONE,
            "-r": FLAG_ARG_STRING, "--runstates": FLAG_ARG_STRING,
            "--ns": FLAG_ARG_STRING, "--nslist": FLAG_ARG_STRING,
            "--help": FLAG_ARG_NONE, "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
        },
    },
    "tput": {
        "safeFlags": {
            "-T": FLAG_ARG_STRING, "-V": FLAG_ARG_NONE, "-x": FLAG_ARG_NONE,
        },
    },
    "ss": {
        "safeFlags": {
            "-h": FLAG_ARG_NONE, "--help": FLAG_ARG_NONE,
            "-V": FLAG_ARG_NONE, "--version": FLAG_ARG_NONE,
            "-n": FLAG_ARG_NONE, "--numeric": FLAG_ARG_NONE,
            "-r": FLAG_ARG_NONE, "--resolve": FLAG_ARG_NONE,
            "-a": FLAG_ARG_NONE, "--all": FLAG_ARG_NONE,
            "-l": FLAG_ARG_NONE, "--listening": FLAG_ARG_NONE,
            "-o": FLAG_ARG_NONE, "--options": FLAG_ARG_NONE,
            "-e": FLAG_ARG_NONE, "--extended": FLAG_ARG_NONE,
            "-m": FLAG_ARG_NONE, "--memory": FLAG_ARG_NONE,
            "-p": FLAG_ARG_NONE, "--processes": FLAG_ARG_NONE,
            "-i": FLAG_ARG_NONE, "--info": FLAG_ARG_NONE,
            "-s": FLAG_ARG_NONE, "--summary": FLAG_ARG_NONE,
            "-4": FLAG_ARG_NONE, "--ipv4": FLAG_ARG_NONE,
            "-6": FLAG_ARG_NONE, "--ipv6": FLAG_ARG_NONE,
            "-0": FLAG_ARG_NONE, "--packet": FLAG_ARG_NONE,
            "-t": FLAG_ARG_NONE, "--tcp": FLAG_ARG_NONE,
            "-M": FLAG_ARG_NONE, "--mptcp": FLAG_ARG_NONE,
            "-S": FLAG_ARG_NONE, "--sctp": FLAG_ARG_NONE,
            "-u": FLAG_ARG_NONE, "--udp": FLAG_ARG_NONE,
            "-d": FLAG_ARG_NONE, "--dccp": FLAG_ARG_NONE,
            "-w": FLAG_ARG_NONE, "--raw": FLAG_ARG_NONE,
            "-x": FLAG_ARG_NONE, "--unix": FLAG_ARG_NONE,
            "--tipc": FLAG_ARG_NONE, "--vsock": FLAG_ARG_NONE,
            "-f": FLAG_ARG_STRING, "--family": FLAG_ARG_STRING,
            "-A": FLAG_ARG_STRING, "--query": FLAG_ARG_STRING,
            "--socket": FLAG_ARG_STRING,
            "-Z": FLAG_ARG_NONE, "--context": FLAG_ARG_NONE,
            "-z": FLAG_ARG_NONE, "--contexts": FLAG_ARG_NONE,
            "-b": FLAG_ARG_NONE, "--bpf": FLAG_ARG_NONE,
            "-E": FLAG_ARG_NONE, "--events": FLAG_ARG_NONE,
            "-H": FLAG_ARG_NONE, "--no-header": FLAG_ARG_NONE,
            "-O": FLAG_ARG_NONE, "--oneline": FLAG_ARG_NONE,
            "--tipcinfo": FLAG_ARG_NONE, "--tos": FLAG_ARG_NONE,
            "--cgroup": FLAG_ARG_NONE, "--inet-sockopt": FLAG_ARG_NONE,
        },
    },
    "fd": {"safeFlags": {**_FD_SAFE_FLAGS}},
    "fdfind": {"safeFlags": {**_FD_SAFE_FLAGS}},
    **PYRIGHT_READ_ONLY_COMMANDS,
    **DOCKER_READ_ONLY_COMMANDS,
}


# ---------------------------------------------------------------------------
# Simple read-only commands (regex-based, from READONLY_COMMANDS)
# ---------------------------------------------------------------------------
# These commands have no dangerous flags — all flags are safe, so we use
# a simple regex that blocks shell metacharacters.

_READ_ONLY_SIMPLE_COMMANDS = [
    # Cross-platform
    *EXTERNAL_READONLY_COMMANDS,
    # Time and date
    "cal", "uptime",
    # File content viewing
    "cat", "head", "tail", "wc", "stat", "strings", "hexdump", "od", "nl",
    # System info
    "id", "uname", "free", "df", "du", "locale", "groups", "nproc",
    # Path information
    "basename", "dirname", "realpath",
    # Text processing
    "cut", "paste", "tr", "column", "tac", "rev", "fold",
    "expand", "unexpand", "fmt", "comm", "cmp", "numfmt",
    "readlink", "diff",
    # true/false
    "true", "false",
    # Misc safe commands
    "sleep", "which", "type", "expr", "test", "getconf",
    "seq", "tsort", "pr",
]


def _make_safe_regex(cmd: str) -> re.Pattern:
    """Create a regex that matches safe invocations of a simple command.
    Blocks shell metacharacters, command substitution, variable expansion.
    """
    return re.compile(rf"^{re.escape(cmd)}(?:\s|$)[^<>()$`|{{}}&;\n\r]*$")


# Build regex set for simple read-only commands
READONLY_COMMAND_REGEXES: list[re.Pattern] = [
    _make_safe_regex(cmd) for cmd in _READ_ONLY_SIMPLE_COMMANDS
]

# Additional specific regex patterns
READONLY_COMMAND_REGEXES.extend([
    # echo — no command substitution, no variables in double quotes
    re.compile(r"""^echo(?:\s+(?:'[^']*'|"[^"$<>\n\r]*"|[^|;&`$(){}><#\\!"'\s]+))*(?:\s+2>&1)?\s*$"""),
    # pwd
    re.compile(r"^pwd$"),
    # whoami
    re.compile(r"^whoami$"),
    # node version
    re.compile(r"^node -v$"),
    re.compile(r"^node --version$"),
    # python version
    re.compile(r"^python --version$"),
    re.compile(r"^python3 --version$"),
    # history
    re.compile(r"^history(?:\s+\d+)?\s*$"),
    # alias
    re.compile(r"^alias$"),
    # arch
    re.compile(r"^arch(?:\s+(?:--help|-h))?\s*$"),
    # ip addr
    re.compile(r"^ip addr$"),
    # ifconfig
    re.compile(r"^ifconfig(?:\s+[a-zA-Z][a-zA-Z0-9_-]*)?\s*$"),
    # ls
    re.compile(r"^ls(?:\s+[^<>()$`|{}&;\n\r]*)?$"),
    # cd
    re.compile(r"""^cd(?:\s+(?:'[^']*'|"[^"]*"|[^\s;|&`$(){}><#\\]+))?$"""),
    # find — block dangerous flags like -delete, -exec
    re.compile(
        r"^find(?:\s+(?:\\[()]|(?!-delete\b|-exec\b|-execdir\b|-ok\b|-okdir\b"
        r"|-fprint0?\b|-fls\b|-fprintf\b)[^<>()$`|{}&;\n\r\s]|\s)+)?$"
    ),
])


# ---------------------------------------------------------------------------
# Safe target commands for xargs
# ---------------------------------------------------------------------------
SAFE_TARGET_COMMANDS_FOR_XARGS = {"echo", "printf", "wc", "grep", "head", "tail"}


# ---------------------------------------------------------------------------
# Flag validation
# ---------------------------------------------------------------------------

# Pattern to detect flag tokens
_FLAG_PATTERN = re.compile(r"^(--?[a-zA-Z][\w-]*)(=(.*))?$")


def validate_flags(
    tokens: list[str],
    start_idx: int,
    safe_flags: dict[str, str],
    *,
    command_name: str = "",
    xargs_target_commands: Optional[set[str]] = None,
) -> bool:
    """Validate that all flags from start_idx onwards are in the safe_flags allowlist.

    Returns True if all flags are safe, False if any unknown or dangerous flag found.
    """
    i = start_idx
    flags_with_args: set[str] = set()
    # Pre-compute which flags take arguments
    for flag, arg_type in safe_flags.items():
        if arg_type != FLAG_ARG_NONE:
            flags_with_args.add(flag)

    has_double_dash = False

    while i < len(tokens):
        token = tokens[i]
        if token is None:
            i += 1
            continue

        # End of options marker
        if token == "--":
            has_double_dash = True
            i += 1
            continue

        # After --, remaining tokens are positional args
        if has_double_dash:
            i += 1
            continue

        # Check for flag tokens
        if token.startswith("-"):
            # Git numeric shorthand: -N is equivalent to -n N for git log/stash show
            if (command_name == "git"
                    and re.match(r"^-\d+$", token)
                    and "-n" in safe_flags):
                i += 1
                continue

            flag_match = _FLAG_PATTERN.match(token)
            if flag_match:
                flag_name = flag_match.group(1)
                fused_value = flag_match.group(3)

                if flag_name in safe_flags:
                    arg_type = safe_flags[flag_name]
                    if arg_type == FLAG_ARG_NONE:
                        # No argument needed
                        i += 1
                        continue
                    elif fused_value is not None:
                        # Value is fused with the flag (--flag=value or -fvalue)
                        i += 1
                        continue
                    else:
                        # Need a separate argument
                        i += 1
                        if i < len(tokens):
                            i += 1
                            continue
                        else:
                            return False  # Missing argument
                else:
                    # Unknown flag
                    return False

            # Handle combined short flags (e.g., -la)
            if token.startswith("-") and not token.startswith("--") and len(token) > 1:
                # Check if this is a combined short flags bundle
                all_known = True
                for ch in token[1:]:
                    short_flag = f"-{ch}"
                    if short_flag not in safe_flags:
                        all_known = False
                        break
                    if safe_flags[short_flag] != FLAG_ARG_NONE:
                        all_known = False
                        break
                if all_known and len(token) > 2:
                    i += 1
                    continue

            # If we get here, unknown flag
            if token.startswith("-"):
                # Check if maybe a known flag with attached value (short form)
                for known_flag in flags_with_args:
                    if token.startswith(known_flag) and len(token) > len(known_flag):
                        # Attached argument form (e.g., -m50 for -m 50)
                        if known_flag in safe_flags:
                            i += 1
                            break
                else:
                    return False
                continue

            return False
        else:
            # Positional argument — for xargs, check target command
            if command_name == "xargs" and xargs_target_commands:
                if token in xargs_target_commands:
                    # Safe target found, rest is the target command's args
                    break
                else:
                    return False
            # Regular positional argument, skip
            i += 1

    return True


# ---------------------------------------------------------------------------
# is_command_safe_via_flag_parsing — the main allowlist checker
# ---------------------------------------------------------------------------

def is_command_safe_via_flag_parsing(command: str) -> bool:
    """Check if a command is safe using the COMMAND_ALLOWLIST.

    Returns True if the command matches an allowlist entry with all safe flags.
    """
    tokens = _simple_tokenize(command)
    if not tokens:
        return False

    # Find matching command config (check multi-word commands first)
    config = None
    cmd_token_count = 0

    # Sort by length descending to match longest prefix first
    sorted_cmds = sorted(COMMAND_ALLOWLIST.keys(), key=len, reverse=True)
    for cmd_pattern in sorted_cmds:
        cmd_parts = cmd_pattern.split()
        if len(tokens) >= len(cmd_parts):
            if all(tokens[j] == cmd_parts[j] for j in range(len(cmd_parts))):
                config = COMMAND_ALLOWLIST[cmd_pattern]
                cmd_token_count = len(cmd_parts)
                break

    if config is None:
        return False

    safe_flags = config.get("safeFlags", {})

    # Reject tokens containing $ (variable expansion bypass)
    for token in tokens[cmd_token_count:]:
        if token and "$" in token:
            return False
        # Reject brace expansion patterns
        if token and "{" in token and ("," in token or ".." in token):
            return False

    # Validate flags
    if not validate_flags(
        tokens, cmd_token_count, safe_flags,
        command_name=tokens[0] if tokens else "",
        xargs_target_commands=SAFE_TARGET_COMMANDS_FOR_XARGS if tokens and tokens[0] == "xargs" else None,
    ):
        return False

    # Check regex constraint if present
    regex = config.get("regex")
    if regex and not regex.match(command):
        return False

    # Check additional callback if present
    callback = config.get("additionalCommandIsDangerousCallback")
    if callback and callback(command, tokens[cmd_token_count:]):
        return False

    return True


# ---------------------------------------------------------------------------
# is_command_read_only — check a single command string
# ---------------------------------------------------------------------------

def is_command_read_only(command: str) -> bool:
    """Check if a single (non-compound) command string is read-only.

    Returns True if the command is safe to auto-approve.
    """
    test_cmd = command.strip()
    # Strip trailing 2>&1
    if test_cmd.endswith(" 2>&1"):
        test_cmd = test_cmd[:-5].strip()

    # Check for unquoted variable/glob expansion
    if _contains_unquoted_expansion(test_cmd):
        return False

    # Check allowlist first (flag-level validation)
    if is_command_safe_via_flag_parsing(test_cmd):
        return True

    # Check regex patterns
    for regex in READONLY_COMMAND_REGEXES:
        if regex.match(test_cmd):
            return True

    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_tokenize(command: str) -> list[str]:
    """Simple shell tokenizer — splits on whitespace respecting basic quoting.

    This is a simplified tokenizer for flag validation. It handles single and
    double quotes but does not handle all shell features (heredocs, etc.).
    For those cases, the command will be rejected by the security checks.
    """
    tokens = []
    current = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == '\\' and not in_single and i + 1 < len(command):
            current.append(command[i + 1])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue
        if ch in (' ', '\t') and not in_single and not in_double:
            if current:
                tokens.append(''.join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        tokens.append(''.join(current))
    return tokens


def _contains_unquoted_expansion(command: str) -> bool:
    """Check for unquoted glob chars (? * [ ]) and expandable $ variables."""
    in_single = False
    in_double = False
    escaped = False

    for i, ch in enumerate(command):
        if escaped:
            escaped = False
            continue
        if ch == '\\' and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single:
            continue
        # Check $ followed by variable-name char
        if ch == '$':
            if i + 1 < len(command) and re.match(r'[A-Za-z_@*#?!$0-9-]', command[i + 1]):
                return True
        if in_double:
            continue
        # Check glob chars outside all quotes
        if ch in ('?', '*', '[', ']'):
            return True

    return False
