
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
            return await _run_doctests(module_path)
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


async def _run_doctests(module_path: str) -> dict:
    """Internal helper to run doctest via subprocess."""
    try:
        # Run doctest using the python module runner and capture output
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "doctest", "-v", module_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode().strip() + "\n" + stderr.decode().strip()

        # Simple heuristic to determine success/failure based on output content
        if "FAILURES" in output or "Failed example" in output:
            summary = f"FAILURE: Doctests failed. Details:\n{output}"
            return {"status": "error", "output": summary, "details": output}
        elif "Ran 0 tests" in output and not output.strip():
             # Handle case where module exists but has no doctests
             summary = "SUCCESS: No doctests found or run."
             return {"status": "success", "output": summary, "details": ""}
        else:
            summary = "SUCCESS: All doctests passed."
            return {"status": "success", "output": summary, "details": output}

    except FileNotFoundError:
        return {"status": "error", "message": f"Module file not found at {module_path}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}



if __name__ == "__main__":
    mcp.run()
