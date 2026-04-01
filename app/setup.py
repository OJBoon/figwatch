from setuptools import setup

APP = ['Sources/FigWatch.py']
OPTIONS = {
    'argv_emulation': False,
    'plist': {
        'CFBundleIdentifier': 'com.joybuy.figwatch',
        'CFBundleName': 'FigWatch',
        'CFBundleDisplayName': 'FigWatch',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'LSUIElement': True,
        'NSHighResolutionCapable': True,
    },
    'packages': ['rumps'],
    'resources': ['Sources/FigWatch-icon.pdf'],
}

setup(
    app=APP,
    name='FigWatch',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
