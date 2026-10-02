import os
from pathlib import Path


def home_directory(environment=None):
    env = os.environ if environment is None else environment
    value = env.get("HOME", "")
    return Path(value) if Path(value).is_absolute() else Path.home()


def xdg_directory(environment, name):
    value = environment.get(name, "")
    if Path(value).is_absolute():
        return Path(value)
    suffix = {"XDG_CONFIG_HOME": ".config", "XDG_STATE_HOME": ".local/state", "XDG_DATA_HOME": ".local/share"}[name]
    return home_directory(environment) / suffix


def persist_root(environment=None):
    return xdg_directory(os.environ if environment is None else environment, "XDG_STATE_HOME") / "fucc"

