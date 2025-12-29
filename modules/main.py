from app.bot_app import run
from core.bot_logging import configure_logging


if __name__ == '__main__':
    configure_logging()
    run()
