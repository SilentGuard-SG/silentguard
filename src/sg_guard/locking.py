"""Serialize this package's inference while a temporary attention observer is installed."""
from threading import RLock

INFERENCE_LOCK = RLock()
