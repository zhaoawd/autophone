class DeliveryError(RuntimeError):
    """Error code shared by optional delivery providers and console clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
