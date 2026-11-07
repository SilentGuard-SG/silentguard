"""SilentGuard: inspect and filter tool returns before agent continuation."""

__version__ = "0.1.0"


def __getattr__(name):
    if name == "SGGuard":
        from .guard import SGGuard
        return SGGuard
    if name == "Engine":
        from .runtime import Engine
        return Engine
    raise AttributeError(name)


__all__ = ["SGGuard", "Engine", "__version__"]
