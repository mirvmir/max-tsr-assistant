"""Application facade; presenter imports do not initialize composition."""
__all__ = ["Application"]


def __getattr__(name):
    if name == "Application":
        from .service import Application
        return Application
    raise AttributeError(name)
