"""The installed WinDroid's entry point: WinDroid.exe (no console) and windroid-cli.exe (with one), both
built from windroid.spec. Each does what python -m androiddesktop does with the same arguments."""
from androiddesktop.__main__ import main

if __name__ == "__main__":
    main()
