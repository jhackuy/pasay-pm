import hashlib


def hash_api_key(api_key: str) -> str:
    """Deterministic hash used to store/compare API keys.

    Plain keys are never stored; only the SHA-256 digest is kept.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()
