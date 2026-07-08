import pytest
import sanitize


def test_keeps_normal_names():
    assert sanitize.branch("feat/db-schema") == "feat/db-schema"
    assert sanitize.worktree_id("pr-01") == "pr-01"


def test_rejects_shell_metacharacters():
    with pytest.raises(ValueError):
        sanitize.branch("feat/x; rm -rf ~")
    with pytest.raises(ValueError):
        sanitize.branch("feat/$(whoami)")


def test_rejects_empty_or_dotdot():
    with pytest.raises(ValueError):
        sanitize.worktree_id("../escape")
    with pytest.raises(ValueError):
        sanitize.branch("")
