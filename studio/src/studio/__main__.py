"""`python -m studio up` is `slng studio up`."""

import sys

from studio.cli import main

sys.exit(main(['studio', *sys.argv[1:]]))
