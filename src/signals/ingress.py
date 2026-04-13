"""Transport-agnostic ingress protocol (HTTP implementation in http_ingress)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SignalIngress(Protocol):
    def start(self) -> None:
        """Begin accepting / forwarding signals (non-blocking)."""

    def stop(self) -> None:
        """Stop listener and join worker."""
