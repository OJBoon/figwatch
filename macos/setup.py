"""py2app build configuration for the FigWatch macOS app.

Usage:
    cd macos
    python3.11 setup.py py2app

The built app will be at macos/dist/FigWatch.app.
"""

import sys
import os

# Add repo root to sys.path so py2app can find the figwatch package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from setuptools import setup

APP = ['FigWatch.py']
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'AppIcon.icns',
    'packages': ['figwatch'],
    'plist': {
        'CFBundleName': 'FigWatch',
        'CFBundleDisplayName': 'FigWatch',
        'CFBundleIdentifier': 'com.figwatch.app',
        'CFBundleVersion': '1.2.0',
        'CFBundleShortVersionString': '1.2.0',
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
