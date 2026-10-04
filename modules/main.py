import sys

from app.bot_app import run
from core.bot_logging import configure_logging


if __name__ == '__main__':
    if sys.argv[1:] == ['--check']:
        from app.smoke_check import run_smoke_check

        sys.exit(run_smoke_check())
    configure_logging()
    run()
