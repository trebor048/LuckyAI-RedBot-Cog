"""UI helpers for Lucky AI.

Keep imports lazy so package discovery does not require the Discord runtime.
"""

__all__ = ["SettingsView"]


def __getattr__(name):
    if name == "SettingsView":
        from .settings import SettingsView

        return SettingsView
    raise AttributeError(name)
