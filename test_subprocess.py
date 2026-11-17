import asyncio
import unittest
import sys

DEFAULT_TIMEOUT_SECONDS = 180
MAX_TIMEOUT_SECONDS = 3600


def validate_timeout(timeout: int) -> int:
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be an integer between 1 and {MAX_TIMEOUT_SECONDS}")
    return timeout


async def run_subprocess(*cmd: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run a subprocess, killing it at the finite timeout while retaining all output."""
    try:
        validate_timeout(timeout_seconds)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        communication = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, stderr = await communication

        stdout_text = stdout.decode(errors="replace").strip()
        stderr_text = stderr.decode(errors="replace").strip()
        output = "\n".join(part for part in [stdout_text, stderr_text] if part)
        return {
            "status": "timeout" if timed_out else ("success" if process.returncode == 0 else "error"),
            "timed_out": timed_out,
            "returncode": process.returncode,
            "output": output,
            "stdout": stdout_text,
            "stderr": stderr_text,
        }
    except FileNotFoundError as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

class RunSubprocessTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserves_output_and_reports_status(self):
        result = await run_subprocess(
            sys.executable, "-c",
            "import sys,time; print('before', flush=True); print('problem', file=sys.stderr, flush=True); time.sleep(10)",
            timeout_seconds=1,
        )
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["timed_out"])
        self.assertIsNotNone(result["returncode"])
        self.assertIn("before", result["stdout"])
        self.assertIn("problem", result["stderr"])

    async def test_rejects_invalid_timeout(self):
        result = await run_subprocess(sys.executable, "-c", "pass", timeout_seconds=0)
        self.assertEqual(result["status"], "error")
        self.assertIn("timeout_seconds", result["message"])


async def run_subprocess_checked(*cmd: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Run a subprocess command and raise RuntimeError on non-zero exit status."""
    result = await run_subprocess(*cmd, timeout_seconds=timeout_seconds)
    if result.get("status") != "success":
        returncode = result.get("returncode")
        output = result.get("output") or result.get("message", "")
        raise RuntimeError(
            f"Command failed ({returncode}): {' '.join(cmd)}\n{output}".strip()
        )
    return result
