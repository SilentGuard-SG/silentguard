"""Load and validate the immutable inference bundle."""
from importlib.resources import files
import hashlib
import json
import numpy as np


def load_assets():
    root = files("sg_guard.assets")
    config = json.loads(root.joinpath("config.json").read_text())
    for name, expected in config["assets"].items():
        if hashlib.sha256(root.joinpath(name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Inference asset checksum mismatch: {name}")
    with root.joinpath("probe.npz").open("rb") as stream:
        with np.load(stream, allow_pickle=False) as data:
            direction = data["direction"].astype(np.float32)
            weights = data["layer_weights"].astype(np.float64)
    expected = (config["observed_layers"], config["hidden_size"])
    if direction.shape != expected or weights.shape != (expected[0],):
        raise ValueError("Unexpected inference asset dimensions")
    if not np.isfinite(direction).all() or not np.isfinite(weights).all():
        raise ValueError("Nonfinite inference asset")
    if (weights < 0).any() or weights.sum() <= 0:
        raise ValueError("Layer weights must be nonnegative with positive sum")
    if not np.allclose(np.linalg.norm(direction, axis=1), 1.0, atol=2e-5):
        raise ValueError("Probe vectors must have unit length")
    weights = weights / weights.sum()
    return config, direction, weights, root.joinpath("chat_template.jinja").read_text()
