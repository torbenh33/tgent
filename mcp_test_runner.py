from fastmcp import FastMCP
import os
import sys

from test_subprocess import run_subprocess

mcp = FastMCP("TestRunnerService")


@mcp.resource("unittests://{modpath}")
async def run_unittests(modpath: str) -> dict:
    """Run unittest discovery for a given test module file.
       + some stuff"""
    if not modpath:
        return {"status": "error", "message": "Module path must be provided."}
    if not os.path.exists(modpath):
        return {"status": "error", "message": f"Module file not found at {modpath}"}

    test_dir = modpath or "."
    pattern = os.path.basename(modpath)

    return await run_subprocess(
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        test_dir,
        "-p",
@mcp.resource("pytest://{project_dir}")
async def run_pytest(project_dir: str) -> dict:
    """Run pytest discovery, defaulting an empty project directory to the current directory."""
    target_dir = project_dir.strip() if isinstance(project_dir, str) and project_dir.strip() else "."

    return await run_subprocess(
        sys.executable,
        "-m",
        "pytest",
        "-vv",
        "--rootdir",
        target_dir,
    )


@mcp.resource("pytest://")
async def run_pytest_default() -> dict:
    """Run pytest discovery in the current directory."""
    return await run_pytest(".")
        pattern,
    )


@mcp.resource("doctests://{modpath}")
async def run_doctests(modpath: str) -> dict:
    """Run doctests for a given module file."""
    if not modpath:
        return {"status": "error", "message": "Module path must be provided."}
    if not os.path.exists(modpath):
        return {"status": "error", "message": f"Module file not found at {modpath}"}

    return await run_subprocess(sys.executable, "-m", "doctest", "-v", modpath)


if __name__ == "__main__":
    mcp.run()
