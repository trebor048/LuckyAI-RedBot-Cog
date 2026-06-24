"""Command helpers for Lucky AI.

Lazy exports keep package imports lightweight so test discovery and tooling
do not need the Discord runtime loaded unless the concrete modules are used.
"""

__all__ = ["AdminCommands", "SetupView", "ensure_config_json"]


def __getattr__(name):
    if name == "AdminCommands":
        from .admin import AdminCommands

        return AdminCommands
    if name in {"SetupView", "ensure_config_json"}:
        from .setup import SetupView, ensure_config_json

        return {"SetupView": SetupView, "ensure_config_json": ensure_config_json}[name]
    raise AttributeError(name)
