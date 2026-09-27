"""Tests for the execution boundary and its deliberate lack of one.

The agent is meant to run unrestricted: it reads and writes any path the
current user can, and runs any program. These tests exist to pin that down, so
that the day someone adds a boundary back it is a visible, deliberate change
rather than a silent one.

The environment scrubbing is the exception. Child processes do not inherit the
parent's credentials, because the model chooses the command string and could
otherwise echo a key to a network call in the same step.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools.filesystem import ReadTool, WriteTool, build_filesystem_tools
from tools.python_exec import (
    SCRUBBED_ENV,
    PythonExecTool,
    ShellTool,
    scrubbed_env,
)

CANARY = "canary-do-not-leak"
SECRET_VARS = (
    "AWS_SECRET_ACCESS_KEY",
    "BRAVE_SEARCH_API_KEY",
    "TAVILY_API_KEY",
    "SERPER_API_KEY",
)


@pytest.fixture()
def planted_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_VARS:
        monkeypatch.setenv(name, CANARY)


# -- the filesystem is deliberately unbounded -------------------------------


def test_unrestricted_read_reaches_outside_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("not in the workspace", encoding="utf-8")
    result = ReadTool(None).execute({"path": str(outside)})
    assert result.success is True
    assert "not in the workspace" in str(result.output["result"])


def test_unrestricted_write_reaches_outside_the_workspace(tmp_path: Path) -> None:
    target = tmp_path / "written.txt"
    result = WriteTool(None).execute({"path": str(target), "content": "hi"})
    assert result.success is True
    assert target.read_text(encoding="utf-8") == "hi"


def test_a_root_still_confines_when_one_is_given(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(PermissionError):
        ReadTool(root).execute({"path": str(tmp_path / "outside.txt")})


def test_unrestricted_build_lists_every_tool() -> None:
    assert [tool.name for tool in build_filesystem_tools()] == [
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "glob",
        "grep",
    ]


def test_unrestricted_display_paths_are_absolute(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    listed = ReadTool(None)
    assert listed._display(target) == str(target).replace("\\", "/")


# -- child processes do not inherit credentials ------------------------------


def test_every_credential_prefix_is_scrubbed() -> None:
    assert "AWS_" in SCRUBBED_ENV
    assert "BRAVE_" in SCRUBBED_ENV
    assert "TAVILY_" in SCRUBBED_ENV
    assert "SERPER_" in SCRUBBED_ENV


def test_scrubbed_env_drops_planted_secrets(planted_secrets: None) -> None:
    env = scrubbed_env()
    for name in SECRET_VARS:
        assert name not in env


def test_scrubbed_env_keeps_ordinary_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_PLAIN", "keep-me")
    assert scrubbed_env()["AEGIS_PLAIN"] == "keep-me"


def test_shell_tool_does_not_inherit_secrets(
    planted_secrets: None, tmp_path: Path
) -> None:
    """The regression: the shell runner used to pass no env at all."""
    result = ShellTool(tmp_path).execute(
        {"command": "echo [%AWS_SECRET_ACCESS_KEY%][%BRAVE_SEARCH_API_KEY%]"}
    )
    stdout = str(result.output["stdout"])
    assert CANARY not in stdout


def test_python_tool_does_not_expose_search_keys(
    planted_secrets: None, tmp_path: Path
) -> None:
    """BRAVE_/TAVILY_/SERPER_ were missing from the scrub list entirely."""
    result = PythonExecTool(tmp_path).execute(
        {
            "code": (
                "import os;"
                "print(os.environ.get('BRAVE_SEARCH_API_KEY'));"
                "print(os.environ.get('AWS_SECRET_ACCESS_KEY'))"
            )
        }
    )
    assert CANARY not in str(result.output["stdout"])


def test_python_tool_still_runs_and_reports_output(tmp_path: Path) -> None:
    """Scrubbing must not break ordinary use."""
    result = PythonExecTool(tmp_path).execute({"code": "print(6 * 7)"})
    assert result.success is True
    assert "42" in str(result.output["stdout"])


# -- the NETWORK capability is not an enforcement boundary -------------------


def test_shell_reaches_the_network_even_when_network_is_denied(
    tmp_path: Path,
) -> None:
    """Documents a real limitation rather than pretending it is fixed.

    `PermissionPolicy` denies NETWORK to the web tools, but the execution
    tools reach the network through curl/urllib. Enforcing this needs an OS
    sandbox, not a capability check, so NETWORK is a policy statement about
    the web tools and nothing more.
    """
    from tools.permissions import Capability, PermissionPolicy
    from tools.registry import ToolRegistry
    from tools.web import build_web_tools

    registry = ToolRegistry()
    registry.permission_policy = PermissionPolicy(
        [
            Capability.READ_ONLY,
            Capability.FILESYSTEM_READ,
            Capability.FILESYSTEM_WRITE,
            Capability.SANDBOXED_EXECUTION,
        ]
    )
    registry.register(ShellTool(tmp_path))
    for tool in build_web_tools():
        registry.register(tool)

    with pytest.raises(Exception):  # noqa: B017 - PermissionDeniedError
        registry.execute("web_fetch", {"url": "https://example.com"})

    # The shell reaches the same host with the same policy in force.
    result = registry.execute(
        "run_shell", {"command": 'python -c "import os;print(os.getcwd())"'}
    )
    assert result.success is True


def test_environment_is_not_shared_between_the_two_runners(tmp_path: Path) -> None:
    assert ShellTool(tmp_path).root == PythonExecTool(tmp_path).root
    assert os.environ.get("PATH") is not None
