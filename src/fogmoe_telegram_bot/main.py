import sys

from fogmoe_telegram_bot.app.bot_app import run
from fogmoe_telegram_bot.core.bot_logging import configure_logging


def main() -> None:
    if sys.argv[1:] == ['--check']:
        from fogmoe_telegram_bot.app.smoke_check import run_smoke_check

        sys.exit(run_smoke_check())
    configure_logging()
    run()


if __name__ == '__main__':
    main()
