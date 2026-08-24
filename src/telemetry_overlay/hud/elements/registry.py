"""Element type registry, so presets can name element types as strings."""

from __future__ import annotations

from ..preset import ElementConfig
from .base import HudElement, RenderContext

_REGISTRY: dict[str, type[HudElement]] = {}


def register(cls: type[HudElement]) -> type[HudElement]:
    """Class decorator adding an element type to the registry."""
    if not cls.type_name:
        raise ValueError(f"{cls.__name__} must define a type_name")
    if cls.type_name in _REGISTRY:
        raise ValueError(f"element type {cls.type_name!r} is already registered")
    _REGISTRY[cls.type_name] = cls
    return cls


def element_types() -> dict[str, type[HudElement]]:
    return dict(_REGISTRY)


def build_element(config: ElementConfig, ctx: RenderContext) -> HudElement:
    try:
        cls = _REGISTRY[config.type]
    except KeyError:
        raise ValueError(
            f"unknown element type {config.type!r}; known types: "
            f"{', '.join(sorted(_REGISTRY))}"
        ) from None
    return cls(config, ctx)
