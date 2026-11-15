# BallSheet on Windows

The same code runs on Windows, macOS and Linux. BallSheet needs **Python 3.10 or newer**:

    winget install --id Python.Python.3.13 -e --scope user

Then install with PowerShell:

    git clone https://github.com/zklaas/ballsheet.git BallSheet
    cd BallSheet
    powershell -ExecutionPolicy Bypass -File install.ps1

That creates `.venv` (PyQt6), a `ballsheet` command on your PATH (open a
new terminal first), and a **BallSheet** Start-menu entry.

Settings are kept in `%APPDATA%\BallSheet`, downloaded data in `%LOCALAPPDATA%\BallSheet`.

To remove it: delete the folder, the Start-menu entry (BallSheet) and
`%LOCALAPPDATA%\Programs\BallSheet`.

Windows support has not yet been tested on a Windows machine; please report anything
that does not work.
