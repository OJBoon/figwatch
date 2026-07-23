"""py2app build configuration for the FigWatch macOS app.

Usage:
    cd macos
    python3.11 setup.py py2app

The built app will be at macos/dist/FigWatch.app.
"""

import importlib.util
import os
import sys

# Add repo root to sys.path so py2app can find the figwatch package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setuptools import setup

from figwatch import __version__

# Runtime dependencies for the no-CLI gateway path, which drives the company
# Claude gateway through the Anthropic Messages API directly (no `claude` CLI).
# This is the full transitive runtime set of the `anthropic` package (0.118.x).
# py2app's modulegraph does not reliably discover these — anthropic has lazy /
# optional imports (bedrock, vertex) and several deps ship compiled extensions —
# so we list them explicitly. Packages (dirs, incl. those with .so files) go in
# 'packages' for a recursive copy; single-module deps go in 'includes'.
ANTHROPIC_PACKAGES = [
    'anthropic',
    'anyio',
    'sniffio',
    'distro',
    'httpx',
    'httpcore',
    'h11',
    'idna',
    'jiter',              # compiled extension (_jiter)
    'pydantic',
    'pydantic_core',      # compiled extension (_pydantic_core)
    'annotated_types',
    'docstring_parser',
    'typing_inspection',
]
ANTHROPIC_INCLUDES = [
    'typing_extensions',  # single-module dependency
]

# `exceptiongroup` is an anyio dependency only on Python < 3.11. Include it only
# when it is actually installed so the build does not break on a newer build
# interpreter where it is neither needed nor present.
_conditional_packages = [
    name for name in ['exceptiongroup']
    if importlib.util.find_spec(name) is not None
]

APP = ['FigWatch.py']
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'AppIcon.icns',
    'packages': ['figwatch', 'certifi', *ANTHROPIC_PACKAGES, *_conditional_packages],
    'includes': ANTHROPIC_INCLUDES,
    'plist': {
        'CFBundleName': 'FigWatch',
        'CFBundleDisplayName': 'FigWatch',
        'CFBundleIdentifier': 'com.figwatch.app',
        'CFBundleVersion': __version__,
        'CFBundleShortVersionString': __version__,
        'LSUIElement': True,           # Menu bar app — no Dock icon
        'NSHighResolutionCapable': True,
    },
}

setup(
    name='FigWatch',
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
