from logging.config import dictConfig

from src.core.config import settings

DEV_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d - %(message)s"
PROD_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging():
    log_level = "DEBUG" if settings.DEBUG else "INFO"

    if settings.ENVIRONMENT == "local":
        formatter_class = "logging.Formatter"
        formatter_format = DEV_LOG_FORMAT
    else:
        formatter_class = "pythonjsonlogger.jsonlogger.JsonFormatter"
        formatter_format = PROD_LOG_FORMAT

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": formatter_class,
                "format": formatter_format,
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "level": "DEBUG",
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
        },
        "loggers": {
            "src": {
                "handlers": ["console"],
                "level": log_level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["console"],
                "level": "INFO",
                "propagate": False,
            },
            "sqlalchemy.engine": {
                "handlers": ["console"],
                "level": "WARNING",
                "propagate": False,
            },
        },
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
    }

    dictConfig(logging_config)
