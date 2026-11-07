from app.core.config import Settings


def test_billing_provider_settings_are_declared() -> None:
    settings = Settings(
        yookassa_shop_id="shop",
        yookassa_secret_key="secret",
        nowpayments_api_key="api-key",
        nowpayments_ipn_secret="ipn-secret",
        payment_webhook_base_url="https://demo.invalid",
    )

    assert settings.yookassa_api_url == "https://api.yookassa.ru/v3"
    assert settings.yookassa_vat_code == 1
    assert settings.nowpayments_api_url == "https://api.nowpayments.io"
    assert settings.payment_webhook_base_url == "https://demo.invalid"
    assert settings.yookassa_shop_id == "shop"
    assert settings.nowpayments_api_key == "api-key"
