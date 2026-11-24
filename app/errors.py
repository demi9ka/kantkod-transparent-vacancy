class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class ModelResponseError(ServiceError):
    """Ответ модели невозможно проверить; можно пропустить текущую вакансию."""
