import PyInstaller.__main__
import sys

# Windows frozen build script for QuizSage.
# --console (keeps console for this first build to catch errors; can be flipped to --windowed later)
# --collect-all nicegui (bundles NiceGUI's static assets)
# --collect-all playwright (bundles Playwright's Node driver binary)

if __name__ == "__main__":
    PyInstaller.__main__.run([
        'gui.py',
        '--name=QuizSage',
        '--onefile',
        '--console', 
        '--collect-all=nicegui',
        '--collect-all=playwright',
        '--noconfirm',
    ])
