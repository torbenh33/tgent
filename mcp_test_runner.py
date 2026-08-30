
from  fastmcp import FastMCP
import unittest
import doctest
import io
import sys
import asyncio
from typing import Literal

mcp = FastMCP("TestRunnerService")

@mcp.tool
async def run_tests(module_path: str, test_type: Literal["unit", "doctest"]) -> dict:
    """
    Runs either unittest discovery or doctests on the specified module path.

    Args:
        module_path: The file path to the Python module containing tests.
        test_type: Must be 'unit' for unittest or 'doctest'.

    Returns:
        A dictionary containing the status and output of the test run.
    """
    if not module_path:
        return {"status": "error", "message": "Module path must be provided."}

    print(f"--- Running {test_type} tests for {module_path} ---")
    
    try:
        if test_type == "unit":
            return await _run_unit_tests(module_path)
        elif test_type == "doctest":
            return await run_doctests(module_path)
        else:
            return {"status": "error", "message": f"Unknown test type: {test_type}. Must be 'unit' or 'doctest'."}

    except Exception as e:
        return {"status": "error", "message": f"An unexpected error occurred during testing: {str(e)}"}


async def _run_unit_tests(module_path: str) -> dict:
    """Internal helper to run unittest."""
    try:
        import importlib.util
        spec = importlib.util.find_spec(module_path)
        if spec is None:
            return {"status": "error", "message": f"Module not found at {module_path}"}

        # Load the module dynamically
        module = importlib.util.module_from_spec(spec)
        sys.modules[module.__name__] = module
        exec(open(module_path).read(), module.__dict__)

        # Discover and run tests
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(module)
        runner = unittest.TextTestRunner(stream=io.StringIO())
        result = runner.run(suite)

        summary = f"Ran {result.countTests()} test(s) in {result.time():.2f} seconds.\n"
        if result.wasSuccessful():
            summary += "SUCCESS: All unit tests passed."
        else:
            summary += f"FAILURE: {len(result._failures)} failure(s), {len(result._errors)} error(s)."

        return {"status": "success", "output": summary}

    except Exception as e:
        return {"status": "error", "message": str(e)}


async def _run_subprocess(*cmd: str) -> dict:
    """Run a subprocess command and return generic execution details."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        stdout_text = stdout.decode().strip()
        stderr_text = stderr.decode().strip()
        output = "\n".join(part for part in [stdout_text, stderr_text] if part)

        return {
            "status": "success" if process.returncode == 0 else "error",
            "returncode": process.returncode,
            "output": output,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
    except FileNotFoundError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.resource("doctests://{modpath}")
async def run_doctests(modpath: str) -> dict:
    """Run doctests and return generic subprocess results."""
    return await _run_subprocess(sys.executable, "-m", "doctest", "-v", modpath)



if __name__ == "__main__":
    mcp.run()
