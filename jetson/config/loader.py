"""Config YAML I/O and dotted-path mutation."""
import os
import yaml

from jetson.constants import CONFIG_PATH, MODELS_DIR
from jetson.config.defaults import DEFAULT_CONFIG


def _deep_update(base, overrides):
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def deep_copy(d):
    if isinstance(d, dict):
        return {k: deep_copy(v) for k, v in d.items()}
    if isinstance(d, list):
        return list(d)
    return d


def load_config():
    config = deep_copy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = yaml.safe_load(f) or {}
            _deep_update(config, user)
            print(f"[CONFIG] Loaded {CONFIG_PATH}")
        except Exception as e:
            print(f"[CONFIG] Failed to load {CONFIG_PATH}: {e}")
    else:
        print(f"[CONFIG] {CONFIG_PATH} not found, using built-in defaults")
    return config


def save_config(config, path=CONFIG_PATH):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(config, f, sort_keys=False, default_flow_style=None)
    print(f"[CONFIG] Saved to {path}")


def list_models():
    """Return sorted list of model file paths in MODELS_DIR."""
    if not os.path.isdir(MODELS_DIR):
        return []
    out = []
    for f in sorted(os.listdir(MODELS_DIR)):
        if f.lower().endswith((".pt", ".engine", ".onnx")):
            out.append(os.path.join(MODELS_DIR, f))
    return out


def set_nested(d, dotted_path, value):
    """Set d[a][b][c] = value from 'a.b.c'. Coerces value to existing type."""
    keys = dotted_path.split(".")
    for k in keys[:-1]:
        d = d[k]
    last = keys[-1]
    existing = d.get(last)
    try:
        if isinstance(existing, bool):
            d[last] = str(value).lower() in ("1", "true", "yes", "on")
        elif isinstance(existing, int) and not isinstance(existing, bool):
            d[last] = int(float(value))
        elif isinstance(existing, float):
            d[last] = float(value)
        elif isinstance(existing, list):
            d[last] = [s.strip() for s in str(value).split(",") if s.strip()]
        else:
            d[last] = value
    except (ValueError, TypeError) as e:
        raise ValueError(f"Bad value for {dotted_path}: {value!r} ({e})")
