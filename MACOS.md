# BallSheet on macOS

The same code runs on macOS, Windows and Linux.

## Where to type the commands

Everything below is typed into **Terminal**: open Applications > Utilities >
Terminal, or press ⌘-Space, type *Terminal* and press Return. Press Return after
each line.

## 1. Install Python

BallSheet needs **Python 3.10 or newer** (the `python3` that comes with macOS is
too old). The simplest way is the installer from
https://www.python.org/downloads/macos/ ; or, with Homebrew (https://brew.sh):

    brew install python@3.13

## 2. Get BallSheet

**With git:**

    git clone https://github.com/zklaas/ballsheet.git BallSheet
    cd BallSheet

(The first time, macOS offers to install its command-line tools for `git`: accept,
then run the line again.)

**Without git:** on https://github.com/zklaas/ballsheet click **Code > Download ZIP**;
the Mac unpacks it into your Downloads folder (`ballsheet-main`). In Terminal type
`cd ` (with a space), drag that folder onto the Terminal window, and press Return.

## 3. Install it

    bash install-macos.sh

That creates `.venv` (PyQt6), a `ballsheet` command in `~/.local/bin`, and a
**BallSheet** app in your Applications folder: start it from Launchpad, Spotlight or
the Dock from then on.

Settings and season files are kept in `~/Library/Application Support/BallSheet`, downloaded data in `~/Library/Caches/BallSheet`.

To remove it: delete the folder, `~/Applications/BallSheet.app` and `~/.local/bin/ballsheet`.

If something doesn't work, please write to zklaastest-testing@yahoo.com.
