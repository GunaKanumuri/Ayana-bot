"""engine/handle_reply.py — Shim for integration prompt imports.

The integration prompt wires:
    from engine.handle_reply import handle_reply

This re-exports handle_parent_response under the expected name.
"""

from app.services.conversation import handle_parent_response as handle_reply  # noqa: F401

__all__ = ["handle_reply"]