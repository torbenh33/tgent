import asyncio


async def run_subprocess(*cmd: str) -> dict:
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


async def run_subprocess_checked(*cmd: str) -> dict:
    """Run a subprocess command and raise RuntimeError on non-zero exit status."""
    result = await run_subprocess(*cmd)
    if result.get("status") != "success":
        returncode = result.get("returncode")
        output = result.get("output") or result.get("message", "")
        raise RuntimeError(
            f"Command failed ({returncode}): {' '.join(cmd)}\n{output}".strip()
        )
    return result
