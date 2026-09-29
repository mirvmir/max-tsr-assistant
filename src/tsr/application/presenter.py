"""Russian copy shared by MAX and the trusted CLI emulator."""
from __future__ import annotations
import json
from importlib.resources import files
from collections.abc import Mapping

_MESSAGES = json.loads(files("tsr.application").joinpath("messages.ru.json").read_text(encoding="utf-8"))


def resolve_message(key: str, parameters: Mapping | None = None) -> str:
    parameters = parameters or {}
    if hasattr(parameters, "root"):
        parameters = parameters.root
    template = _MESSAGES.get(key, key)
    try:
        return template.format_map(parameters)
    except (KeyError, ValueError):
        return template
