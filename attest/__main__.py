"""Allow ``python -m attest`` to invoke the CLI."""
import sys
from attest.cli import main

sys.exit(main())
