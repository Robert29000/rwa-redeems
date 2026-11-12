"""Protocol adapters. Importing this package registers all built-in adapters."""

from rwa_redeems.protocols import midas, superstate  # noqa: F401  (register adapters)
from rwa_redeems.protocols.base import AdapterContext, CollectResult, ProtocolAdapter, adapter_class, register

__all__ = ["AdapterContext", "CollectResult", "ProtocolAdapter", "adapter_class", "register"]
