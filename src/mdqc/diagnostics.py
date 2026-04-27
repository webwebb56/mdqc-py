"""Diagnostics: gathers system health for `mdqc doctor` and the web UI.

Pure module — does not require a running service. The CLI shells out to this
directly; the service exposes the same data via /api/diagnostics.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx

from mdqc import __version__ as _agent_version
from mdqc.config import ConfigError, load_config, paths
from mdqc.config.defaults import HTTP_CONNECT_TIMEOUT_S, HTTP_TIMEOUT_S
from mdqc.config.schema import Config
from mdqc.extractor.skyline import is_clickonce_install

log = logging.getLogger(__name__)


CloudAuthMode = Literal["bearer", "mtls", "local-only"]


@dataclass
class TemplateCheck:
    name: str
    path: Path
    exists: bool
    hash: str | None


@dataclass
class InstrumentCheck:
    id: str
    watch_path: Path
    accessible: bool


@dataclass
class DiagnosticsReport:
    agent_version: str
    config_path: Path
    config_ok: bool
    config_error: str | None = None
    skyline_path: Path | None = None
    skyline_version: str | None = None
    skyline_clickonce: bool = False
    templates: list[TemplateCheck] = field(default_factory=list)
    instruments: list[InstrumentCheck] = field(default_factory=list)
    cloud_endpoint: str = ""
    cloud_auth_mode: CloudAuthMode = "local-only"
    cloud_reachable: bool | None = None
    cert_thumbprint_set_but_unsupported: bool = False
    spool_writable: bool = False
    pending_count: int = 0
    failed_count: int = 0
    overall_ok: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_version": self.agent_version,
            "config_path": str(self.config_path),
            "config_ok": self.config_ok,
            "config_error": self.config_error,
            "skyline_path": str(self.skyline_path) if self.skyline_path else None,
            "skyline_version": self.skyline_version,
            "skyline_clickonce": self.skyline_clickonce,
            "templates": [
                {
                    "name": t.name,
                    "path": str(t.path),
                    "exists": t.exists,
                    "hash": t.hash,
                }
                for t in self.templates
            ],
            "instruments": [
                {
                    "id": i.id,
                    "watch_path": str(i.watch_path),
                    "accessible": i.accessible,
                }
                for i in self.instruments
            ],
            "cloud_endpoint": self.cloud_endpoint,
            "cloud_auth_mode": self.cloud_auth_mode,
            "cloud_reachable": self.cloud_reachable,
            "cert_thumbprint_set_but_unsupported": self.cert_thumbprint_set_but_unsupported,
            "spool_writable": self.spool_writable,
            "pending_count": self.pending_count,
            "failed_count": self.failed_count,
            "overall_ok": self.overall_ok,
        }


def _hash_template(path: Path) -> str | None:
    try:
        if path.is_file():
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
    except OSError as exc:
        log.warning("template_hash_failed", extra={"path": str(path), "error": str(exc)})
    return None


def _resolve_template(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_absolute():
        return candidate
    methods = paths.methods_dir() / name
    if methods.exists():
        return methods
    return paths.templates_dir() / name


def _resolve_skyline(cfg: Config | None) -> Path | None:
    from mdqc.extractor.skyline import find_skyline

    explicit: Path | None = None
    if cfg is not None and cfg.skyline.path and cfg.skyline.path.lower() != "auto":
        explicit = Path(cfg.skyline.path)
    return find_skyline(explicit=explicit)


def _spool_writable() -> bool:
    try:
        d = paths.spool_dir()
        d.mkdir(parents=True, exist_ok=True)
        canary = d / ".diagnostics_canary"
        canary.write_text("ok", encoding="utf-8")
        canary.unlink()
        return True
    except OSError:
        return False


def _spool_counts() -> tuple[int, int]:
    pending = paths.spool_pending()
    failed = paths.spool_failed()
    pending_count = 0
    failed_count = 0
    if pending.exists():
        pending_count = sum(
            1 for p in pending.iterdir() if p.is_file() and p.suffix == ".json"
        )
    if failed.exists():
        failed_count = sum(
            1 for p in failed.iterdir() if p.is_file() and p.suffix == ".json"
        )
    return pending_count, failed_count


def _classify_auth(cfg: Config) -> tuple[CloudAuthMode, bool]:
    cert_unsupported = bool(
        cfg.cloud.certificate_thumbprint and not cfg.cloud.api_token
    )
    if cfg.cloud.api_token:
        return "bearer", cert_unsupported
    if cfg.cloud.certificate_thumbprint:
        return "mtls", cert_unsupported
    return "local-only", cert_unsupported


async def _probe_cloud(endpoint: str) -> bool | None:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT_S, connect=HTTP_CONNECT_TIMEOUT_S),
            trust_env=True,
        ) as client:
            response = await client.get(endpoint)
        return response.status_code < 500
    except httpx.RequestError:
        return False


async def run_diagnostics(*, check_cloud: bool = False) -> DiagnosticsReport:
    config_path = paths.config_path()
    cfg: Config | None = None
    config_ok = False
    config_error: str | None = None

    try:
        cfg = load_config(config_path, strict_cert_guard=False)
        config_ok = True
    except ConfigError as exc:
        config_error = str(exc)
    except OSError as exc:
        config_error = f"failed to read config: {exc}"

    skyline_path = _resolve_skyline(cfg)
    skyline_clickonce = False
    if skyline_path is not None:
        skyline_clickonce = is_clickonce_install(skyline_path)

    templates: list[TemplateCheck] = []
    instruments: list[InstrumentCheck] = []

    if cfg is not None:
        seen_templates: set[Path] = set()
        for inst in cfg.instruments:
            template_path = _resolve_template(inst.template)
            if template_path not in seen_templates:
                seen_templates.add(template_path)
                templates.append(
                    TemplateCheck(
                        name=inst.template,
                        path=template_path,
                        exists=template_path.exists(),
                        hash=_hash_template(template_path) if template_path.exists() else None,
                    )
                )
            watch_path = inst.watch_path
            try:
                accessible = watch_path.exists() and watch_path.is_dir()
            except OSError:
                accessible = False
            instruments.append(
                InstrumentCheck(
                    id=inst.id,
                    watch_path=watch_path,
                    accessible=accessible,
                )
            )

    cloud_endpoint = cfg.cloud.endpoint if cfg is not None else ""
    if cfg is not None:
        cloud_auth_mode, cert_unsupported = _classify_auth(cfg)
    else:
        cloud_auth_mode = "local-only"
        cert_unsupported = False

    cloud_reachable: bool | None = None
    if check_cloud and cfg is not None and cloud_auth_mode != "local-only":
        cloud_reachable = await _probe_cloud(cloud_endpoint)

    spool_writable = _spool_writable()
    pending_count, failed_count = _spool_counts()

    overall_ok = (
        config_ok
        and skyline_path is not None
        and not skyline_clickonce
        and not cert_unsupported
        and any(i.accessible for i in instruments)
    )

    return DiagnosticsReport(
        agent_version=_agent_version,
        config_path=config_path,
        config_ok=config_ok,
        config_error=config_error,
        skyline_path=skyline_path,
        skyline_version=None,
        skyline_clickonce=skyline_clickonce,
        templates=templates,
        instruments=instruments,
        cloud_endpoint=cloud_endpoint,
        cloud_auth_mode=cloud_auth_mode,
        cloud_reachable=cloud_reachable,
        cert_thumbprint_set_but_unsupported=cert_unsupported,
        spool_writable=spool_writable,
        pending_count=pending_count,
        failed_count=failed_count,
        overall_ok=overall_ok,
    )


def run_diagnostics_blocking(*, check_cloud: bool = False) -> DiagnosticsReport:
    return asyncio.run(run_diagnostics(check_cloud=check_cloud))


def _ok(label: str, value: str = "") -> str:
    return f"[OK] {label}{f': {value}' if value else ''}"


def _fail(label: str, value: str = "") -> str:
    return f"[!!] {label}{f': {value}' if value else ''}"


def _muted(label: str, value: str = "") -> str:
    return f"[--] {label}{f': {value}' if value else ''}"


def render_text_report(report: DiagnosticsReport) -> str:
    lines: list[str] = []
    lines.append("MD Local QC Agent - System Health Check")
    lines.append("=" * 40)
    lines.append("")
    lines.append(_ok("Agent version", report.agent_version))
    cfg_label = "Config file"
    cfg_line = _ok(cfg_label, str(report.config_path)) if report.config_ok else _fail(
        cfg_label, str(report.config_path)
    )
    lines.append(cfg_line)
    if report.config_error:
        lines.append(f"    error: {report.config_error}")
    lines.append("")
    lines.append("Skyline")
    lines.append("-" * 7)
    if report.skyline_path is None:
        lines.append(_fail("SkylineCmd.exe", "not found"))
    elif report.skyline_clickonce:
        lines.append(_fail("SkylineCmd.exe", f"{report.skyline_path} (ClickOnce — install MSI)"))
    else:
        lines.append(_ok("SkylineCmd.exe", str(report.skyline_path)))
    if report.skyline_version:
        lines.append(_ok("Skyline version", report.skyline_version))
    else:
        lines.append(_muted("Skyline version", "unknown"))
    lines.append("")
    lines.append("Templates")
    lines.append("-" * 9)
    if not report.templates:
        lines.append(_muted("(no instruments configured)"))
    for t in report.templates:
        if t.exists:
            lines.append(_ok(t.name, str(t.path)))
            if t.hash:
                lines.append(f"    Hash: sha256:{t.hash[:16]}...")
        else:
            lines.append(_fail(t.name, str(t.path)))
    lines.append("")
    lines.append("Instruments")
    lines.append("-" * 11)
    if not report.instruments:
        lines.append(_muted("(none configured)"))
    for inst in report.instruments:
        label = f"{inst.id}: {inst.watch_path}"
        lines.append(
            _ok(label, "accessible") if inst.accessible else _fail(label, "not accessible")
        )
    lines.append("")
    lines.append("Cloud")
    lines.append("-" * 5)
    if report.cert_thumbprint_set_but_unsupported:
        lines.append(_fail("certificate_thumbprint set but mTLS not supported in v1"))
    if report.cloud_auth_mode == "local-only":
        lines.append(_muted("Auth", "local-only (no upload)"))
    else:
        lines.append(_ok("Auth", report.cloud_auth_mode))
    if report.cloud_endpoint:
        lines.append(_ok("Endpoint", report.cloud_endpoint))
    if report.cloud_reachable is True:
        lines.append(_ok("Endpoint reachable"))
    elif report.cloud_reachable is False:
        lines.append(_fail("Endpoint reachable", "no"))
    else:
        lines.append(_muted("Endpoint reachable", "not checked"))
    lines.append("")
    lines.append("Spool")
    lines.append("-" * 5)
    lines.append(
        _ok("Spool directory", "writable")
        if report.spool_writable
        else _fail("Spool directory", "not writable")
    )
    lines.append(_ok("Pending items", str(report.pending_count)))
    lines.append(_ok("Failed items", str(report.failed_count)))
    lines.append("")
    lines.append(f"Overall: {'HEALTHY' if report.overall_ok else 'NEEDS ATTENTION'}")
    return "\n".join(lines)


__all__ = [
    "DiagnosticsReport",
    "InstrumentCheck",
    "TemplateCheck",
    "render_text_report",
    "run_diagnostics",
    "run_diagnostics_blocking",
]
