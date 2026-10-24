from datetime import datetime, timezone
from fastmcp import FastMCP
import os
import sys
import uuid

from test_subprocess import run_subprocess

mcp = FastMCP("TestRunnerService")


def _validate_module_target(modpath: str):
    """Return a resource error for an invalid module path, otherwise None."""
    if not modpath:
        return {"status": "error", "message": "Module path must be provided."}
    if not os.path.exists(modpath):
        return {"status": "error", "message": f"Module file not found at {modpath}"}
    return None

@mcp.resource("unittests://{modpath}")
async def run_unittests(modpath: str) -> dict:
    """Run unittest discovery for a given test module file.
       + some stuff"""
    error = _validate_module_target(modpath)
    if error is not None:
        return error

    test_dir = modpath or "."

    return await run_subprocess(
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        test_dir,
    )

async def _run_pytest(project_dir: str) -> dict:
    """Run pytest and annotate the result with this invocation's identity and context."""
    target_dir = project_dir.strip() if isinstance(project_dir, str) and project_dir.strip() else "."
    started_at = datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())
    command = [sys.executable, "-m", "pytest", "-vv", "--rootdir", target_dir, target_dir]
    result = await run_subprocess(*command)
    completed_at = datetime.now(timezone.utc)
    returncode = result.get("returncode")
    if returncode == 0:
        test_status = "passed"
    elif isinstance(returncode, int):
        test_status = "failed"
    else:
        test_status = "unknown"
    test_run = {
        "id": run_id,
        "fresh_execution": True,
        "cached": False,
        "status": test_status,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "working_directory": os.getcwd(),
        "project_directory": os.path.abspath(target_dir),
        "command": command,
    }
    return {"test_run": test_run, **result}


@mcp.resource("pytest://{project_dir}")
async def run_pytest(project_dir: str) -> dict:
    """Run pytest now and return the result with a unique ID and execution timestamps."""
    return await _run_pytest(project_dir)


@mcp.resource("pytest://")
async def run_pytest_default() -> dict:
    """Run pytest now in the current directory and annotate this fresh execution."""
    return await _run_pytest(".")


@mcp.resource("doctests://{modpath}")
async def run_doctests(modpath: str) -> dict:
    """Run doctests for a given module file."""
    error = _validate_module_target(modpath)
    if error is not None:
        return error

    return await run_subprocess(sys.executable, "-m", "doctest", "-v", modpath)


if __name__ == "__main__":
    mcp.run()
