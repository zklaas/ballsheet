# BallSheet on macOS

The same code runs on macOS, Windows and Linux. BallSheet needs **Python 3.10 or newer**
(macOS's own `python3` is too old):

    brew install python@3.13        # Homebrew: https://brew.sh
    # or the installer from https://www.python.org/downloads/macos/

Then, in Terminal:

    git clone https://github.com/zklaas/ballsheet.git BallSheet
    cd BallSheet
    bash install-macos.sh

That creates `.venv` (PyQt6), a `ballsheet` command in `~/.local/bin`, and a
**BallSheet** app in your Applications folder (Launchpad, Spotlight, the Dock).

Settings and season files are kept in `~/Library/Application Support/BallSheet`, downloaded data in `~/Library/Caches/BallSheet`.

To remove it: delete the folder, `~/Applications/BallSheet.app` and `~/.local/bin/ballsheet`.

macOS support has not yet been tested on a Mac; please report anything that does
not work.
