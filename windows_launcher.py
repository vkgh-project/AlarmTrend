"""PyInstaller entry point for the Windows desktop launcher."""

import sys

from alarmtrend.__main__ import main


if __name__ == '__main__':
    sys.argv = [sys.argv[0], 'desktop', *sys.argv[1:]]
    main()
