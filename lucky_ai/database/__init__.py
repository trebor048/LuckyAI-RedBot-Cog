"""Database helpers for Lucky AI."""

__all__ = ["MessageDB"]


def __getattr__(name):
    if name == "MessageDB":
        from .legacy import MessageDB

        return MessageDB
    raise AttributeError(name)
