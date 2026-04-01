from setuptools import setup
import os

APP = ['FigWatch.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleIdentifier': 'com.joybuy.figwatch',
        'CFBundleName': 'FigWatch',
        'CFBundleDisplayName': 'FigWatch',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1',
        'CFBundleIconFile': 'AppIcon',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    },
    'packages': ['rumps', 'handlers'],
    'resources': ['FigWatch-icon.pdf', 'AppIcon.icns'],
}

setup(
    app=APP,
    name='FigWatch',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
