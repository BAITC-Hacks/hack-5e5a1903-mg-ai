class MLServiceError(Exception):
    """Ожидаемая ошибка. Клиент получает конверт {"error": {"code", "message", "details"}}, как в backend."""

    def __init__(self, status_code: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def invalid_input(code: str, message: str, details: dict | None = None) -> MLServiceError:
    return MLServiceError(422, code, message, details)
