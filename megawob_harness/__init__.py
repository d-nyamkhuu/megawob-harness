"""megawob-harness: coding agents (Codex or Claude) that build MegaWoB design-* sites, gated by the checker."""
from .runtime import AgentError, Runtime, get_runtime

__all__ = ["AgentError", "Runtime", "get_runtime"]
__version__ = "0.2.0"
