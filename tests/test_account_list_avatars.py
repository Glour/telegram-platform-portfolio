import base64
from io import BytesIO

from PIL import Image

from app.modules.telegram_accounts.avatars import account_list_avatar


def test_table_avatar_is_small_and_keeps_original():
    image = Image.effect_noise((800, 800), 80).convert("RGB")
    stream = BytesIO()
    image.save(stream, "JPEG", quality=95)
    original = "data:image/jpeg;base64," + base64.b64encode(stream.getvalue()).decode()
    thumbnail = account_list_avatar(original)
    assert len(thumbnail) < 4096 < len(original)
    decoded = base64.b64decode(thumbnail.split(",", 1)[1])
    with Image.open(BytesIO(decoded)) as result:
        assert result.size == (80, 80)
    assert base64.b64decode(original.split(",", 1)[1]) == stream.getvalue()


def test_small_and_missing_images_are_preserved():
    assert account_list_avatar(None) is None
    assert account_list_avatar("data:image/png;base64,YQ==") == "data:image/png;base64,YQ=="


def test_invalid_large_data_does_not_block_account_list():
    assert account_list_avatar("data:image/png;base64," + "?" * 5000) is None
