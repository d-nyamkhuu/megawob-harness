"""A verifier-gated harness for coding agents."""
from .domain import Domain
from .pipeline import Pipeline, Settings
from .runtime import AgentError, Runtime, get_runtime

__all__ = ["AgentError", "Domain", "Pipeline", "Runtime", "Settings", "get_runtime"]
__version__ = "0.1.0"
