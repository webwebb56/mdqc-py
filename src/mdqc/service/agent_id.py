from __future__ import annotations

import hashlib
import logging
import platform
import socket
import subprocess
import sys
from pathlib import Path

import tomli_w

log = logging.getLogger(__name__)


def _hardware_seed() -> str:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                lines = [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if line.strip() and "UUID" not in line.upper()
                ]
                if lines:
                    return lines[0]
        except (OSError, subprocess.SubprocessError):
            pass
    elif sys.platform.startswith("linux"):
        for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                contents = Path(candidate).read_text(encoding="utf-8").strip()
                if contents:
                    return contents
            except OSError:
                continue
    elif sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                for line in result.stdout.splitlines():
                    if "IOPlatformUUID" in line:
                        parts = line.split('"')
                        if len(parts) >= 4:
                            return parts[-2]
        except (OSError, subprocess.SubprocessError):
            pass

    try:
        return f"{socket.gethostname()}-{platform.machine()}-{platform.system()}"
    except OSError:
        return "mdqc-fallback"


def _derive_agent_id() -> str:
    seed = _hardware_seed()
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return digest[:16]


def resolve_agent_id(value: str, *, persist_to: Path | None = None) -> str:
    if value and value != "auto":
        return value

    derived = _derive_agent_id()

    if persist_to is not None:
        try:
            _persist_agent_id(persist_to, derived)
        except OSError as exc:
            log.warning(
                "agent_id_persist_failed",
                extra={"path": str(persist_to), "error": str(exc)},
            )

    return derived


def _persist_agent_id(config_path: Path, agent_id: str) -> None:
    import tomllib

    with open(config_path, "rb") as fh:
        data = tomllib.load(fh)

    agent = data.get("agent")
    if not isinstance(agent, dict):
        agent = {}
        data["agent"] = agent
    agent["agent_id"] = agent_id

    tmp = config_path.with_name(f".{config_path.name}.tmp")
    with open(tmp, "wb") as fh:
        tomli_w.dump(data, fh)
    import os

    os.replace(tmp, config_path)


__all__ = ["resolve_agent_id"]
