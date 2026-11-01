"""Local captcha-solving toolkit: text / arithmetic / slider / click / rotate."""
from __future__ import annotations

__version__ = "1.0.0"

from . import click, imageops, ocr, pipeline, rotate, slider, text_solver  # noqa: F401

solve = pipeline.solve
DEFAULT_CHARSET = text_solver.DEFAULT_CHARSET

__all__ = [
    "__version__",
    "solve",
    "pipeline",
    "text_solver",
    "slider",
    "click",
    "rotate",
    "ocr",
    "imageops",
    "DEFAULT_CHARSET",
]
