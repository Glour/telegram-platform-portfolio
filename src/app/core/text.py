"""Text helpers shared by application modules."""


def utf16_length(value: str) -> int:
    """Count UTF-16 code units used by Telegram text limits."""
    return len(value.encode("utf-16-le")) // 2
