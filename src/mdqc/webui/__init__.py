"""Web UI hosted by the headless service.

Entry point is `register(app, state)` invoked by
`mdqc.service.lifecycle.attach_webui`. See docs/AGENT_NOTES § Web UI for the
auth + routing contract.
"""

from __future__ import annotations

from mdqc.webui.routes import register

__all__ = ["register"]
