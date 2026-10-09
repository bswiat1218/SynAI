class ProviderError(Exception):
    pass


class ModelUnavailableError(ProviderError):
    """A single model is unavailable while the provider remains reachable."""


class ModelCapabilityMetadataError(ProviderError):
    """A single model's capability response is malformed or incomplete."""
