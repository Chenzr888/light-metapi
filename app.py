"""Compatibility entrypoint for the upstream balance dashboard."""
import sys

from metapi import core as _core

# Keep historic imports and unittest patch targets identical: ``import app``
# must return the actual implementation module, not a copied namespace.
sys.modules[__name__] = _core
