"""Small table avatars; original profile images remain in account metadata."""

import base64
import binascii
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError


@lru_cache(maxsize=64)
def account_list_avatar(data_url: str | None) -> str | None:
    if not data_url or len(data_url) <= 4096:
        return data_url
    if len(data_url) > 10_000_000:
        return None
    try:
        header, encoded = data_url.split(",", 1)
        if not header.startswith("data:image/") or not header.endswith(";base64"):
            return None
        raw = base64.b64decode(encoded, validate=True)
        with Image.open(BytesIO(raw)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((80, 80), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            output = BytesIO()
            rgb.save(output, format="JPEG", quality=65, optimize=True)
        return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
