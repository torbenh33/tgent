"""Read-only MCP tools for inspecting the host's Debian APT configuration.

The server exposes APT source and preference files, manually installed
packages, and package policy information. It never modifies the host. Run it
from the project environment with::

    python mcp_apt.py
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

mcp = FastMCP("apt-host-inspector")
APT_DIR = Path("/etc/apt")
MAX_CONFIG_FILE_SIZE = 1024 * 1024
MAX_POLICY_PACKAGES = 100
PACKAGE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9+._:-]*$")


def _run(command: list[str]) -> dict[str, Any]:
    """Run a fixed system inspection command and return its result."""
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return {"returncode": None, "stdout": "", "stderr": f"Required command not found: {command[0]}"}
    except OSError as exc:
        return {"returncode": None, "stdout": "", "stderr": str(exc)}
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}


def _redact(content: str) -> str:
    """Mask credentials in APT source entries while preserving their structure."""
    content = re.sub(
        r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@",
        r"\1<redacted>@",
        content,
    )
    content = re.sub(
        r"(?i)(\b(?:password|passwd|token|secret|authorization)\s*[:=]\s*)[^\s,;]+",
        r"\1<redacted>",
        content,
    )
    return content


def _read_config_files() -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Read only APT source and preference files from their standard locations."""
    files: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    candidates: list[Path] = [APT_DIR / "sources.list", APT_DIR / "preferences"]
    for directory, suffixes in (
        (APT_DIR / "sources.list.d", {".list", ".sources"}),
        (APT_DIR / "preferences.d", {".pref"}),
    ):
        try:
            candidates.extend(
                path
                for path in sorted(directory.iterdir())
                if path.is_file()
                and (
                    path.suffix in suffixes
                    or (directory.name == "preferences.d" and not path.suffix)
                )
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append({"path": str(directory), "error": str(exc)})

    for path in candidates:
        if not path.exists():
            continue
        try:
            if path.is_symlink():
                errors.append({"path": str(path), "error": "Skipped symlink for safety."})
                continue
            size = path.stat().st_size
            if size > MAX_CONFIG_FILE_SIZE:
                errors.append(
                    {"path": str(path), "error": f"File exceeds {MAX_CONFIG_FILE_SIZE} byte limit."}
                )
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue

        # Do not return empty files; their presence is not useful to the planner.
        if content.strip():
            files.append({"path": str(path), "content": _redact(content)})
    return files, errors


def _keyring_references(config_files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find file-based Signed-By references and report whether they exist."""
    paths: set[str] = set()
    for config in config_files:
        in_signed_by = False
        for line in config["content"].splitlines():
            option_match = re.search(r"(?i)\bsigned-by\s*=\s*([^\]\s]+)", line)
            field_match = re.match(r"(?i)^\s*Signed-By\s*:\s*(.*)$", line)
            values: list[str] = []
            if option_match:
                values.extend(option_match.group(1).split(","))
            elif field_match:
                in_signed_by = True
                values.extend(field_match.group(1).split())
            elif in_signed_by and line[:1].isspace():
                values.extend(line.split())
            else:
                in_signed_by = False
            for value in values:
                if value.startswith("/"):
                    paths.add(value.rstrip(","))

    keyrings: list[dict[str, Any]] = []
    for raw_path in sorted(paths):
        path = Path(raw_path)
        try:
            available = path.is_file() and os.access(path, os.R_OK)
        except OSError:
            available = False
        keyrings.append({"path": raw_path, "available": available})
    return keyrings


def _host_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VERSION_ID", "VERSION_CODENAME", "PRETTY_NAME"}:
                values[key] = value.strip().strip('"')
    except OSError as exc:
        values["error"] = str(exc)
    return values


def _manual_packages() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    manual_proc = _run(["apt-mark", "showmanual"])
    if manual_proc["returncode"] != 0:
        errors.append({"command": "apt-mark showmanual", "error": manual_proc["stderr"]})
        return [], errors
    manual_names = {name.strip() for name in manual_proc["stdout"].splitlines() if name.strip()}

    query_proc = _run(
        [
            "dpkg-query",
            "-W",
            "-f=${binary:Package}\t${Version}\t${Architecture}\t${db:Status-Status}\\n",
        ]
    )
    if query_proc["returncode"] != 0:
        errors.append({"command": "dpkg-query -W", "error": query_proc["stderr"]})
        return [{"name": name} for name in sorted(manual_names)], errors

    packages: list[dict[str, str]] = []
    found: set[str] = set()
    for line in query_proc["stdout"].splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        name, version, architecture, status = fields
        if status != "installed":
            continue
        # apt-mark may report the unqualified name for an installed multiarch package.
        base_name = name.split(":", 1)[0]
        if name in manual_names or base_name in manual_names:
            packages.append(
                {"name": name, "version": version, "architecture": architecture}
            )
            found.add(name)
            found.add(base_name)

    for name in sorted(manual_names - found):
        packages.append({"name": name, "version": "unknown", "architecture": "unknown"})
    packages.sort(key=lambda package: package["name"])
    return packages, errors


@mcp.tool()
def get_host_apt_inventory() -> dict[str, Any]:
    """Scan Debian release, architecture, APT source/preference files, and manually installed packages.

    Only standard source and preference files are read. APT authentication
    files and proxy configuration are never read; credential-like values in
    returned config are redacted. The tool is read-only and reports per-file
    or command errors without discarding the rest of the inventory.
    """
    release = _host_release()
    architecture_proc = _run(["dpkg", "--print-architecture"])
    architecture = architecture_proc["stdout"].strip() if architecture_proc["returncode"] == 0 else None
    sources, config_errors = _read_config_files()
    packages, package_errors = _manual_packages()
    result: dict[str, Any] = {
        "status": "ok",
        "release": release,
        "architecture": architecture,
        "sources_and_preferences": sources,
        "keyring_references": _keyring_references(sources),
        "manual_packages": packages,
    }
    errors = config_errors + package_errors
    if architecture_proc["returncode"] != 0:
        errors.append({"command": "dpkg --print-architecture", "error": architecture_proc["stderr"]})
    if errors:
        result["errors"] = errors
    return result


@mcp.tool()
def get_package_policy(names: list[str]) -> dict[str, Any]:
    """Return apt-cache policy output for up to 100 validated package names.

    The output includes installed/candidate versions and repository origins
    where APT can determine them. Package names are passed as individual
    arguments; arbitrary commands or APT configuration paths are not accepted.
    """
    if not isinstance(names, list):
        return {"status": "error", "message": "names must be a list of package names."}
    selected: list[str] = []
    for name in names:
        if not isinstance(name, str) or not PACKAGE_NAME.fullmatch(name):
            return {"status": "error", "message": f"Invalid package name: {name!r}"}
        if name not in selected:
            selected.append(name)
    if not selected:
        return {"status": "error", "message": "At least one package name must be provided."}
    if len(selected) > MAX_POLICY_PACKAGES:
        return {
            "status": "error",
            "message": f"At most {MAX_POLICY_PACKAGES} package names may be queried at once.",
        }

    proc = _run(["apt-cache", "policy", *selected])
    if proc["returncode"] != 0:
        return {
            "status": "error",
            "message": "apt-cache policy failed.",
            "stderr": proc["stderr"],
            "packages": selected,
        }
    return {
        "status": "ok",
        "packages": selected,
        "policy": _redact(proc["stdout"]),
        "stderr": proc["stderr"],
    }


if __name__ == "__main__":
    mcp.run()
