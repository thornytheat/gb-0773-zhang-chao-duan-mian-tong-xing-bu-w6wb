class TideError(Exception):
    def __init__(self, code, message, status=422, details=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.details = details or {}
