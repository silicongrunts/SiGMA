"""
Unit tests for the bash permission classifier.

Covers the approval-result content shown in the permission dialog: when the
classifier has no richer explanation, the full command must be carried as
``content`` so the user reviews exactly what would run.
"""

from app.agents.tools.bash_permissions import check_bash_permission


def test_needs_approval_content_is_full_command():
    long_command = "rm -f " + " ".join(f"/tmp/artifact-{i}.log" for i in range(200))
    result = check_bash_permission(long_command)
    assert not result.approved
    assert result.content == long_command
