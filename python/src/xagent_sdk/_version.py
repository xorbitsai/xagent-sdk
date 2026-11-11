"""SDK version string.

The version is read from the installed distribution metadata, which the
build derives from the release tag. Importing the package from a source
tree that was never installed (no distribution metadata) yields
``0+unknown``.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("xagent-sdk")
except PackageNotFoundError:
    __version__ = "0+unknown"
