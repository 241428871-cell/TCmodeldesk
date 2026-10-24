"""Proven pre-effect refusals shared by every workbench brain adapter."""


class ReviewRefused(ValueError):
    """Review validation refused before the durable review or task records changed."""

    def __init__(self, detail: str) -> None:
        """Retain the refusal without classifying later failures as safe to replay."""
        self.detail: str = detail
        super().__init__(detail)


class DispatchRefused(ValueError):
    """Dispatch refused before task creation, durable claims or queue submission."""

    def __init__(self, detail: str) -> None:
        """Expose the original safe reason to the journal and human interface."""
        self.detail: str = detail
        super().__init__(detail)
