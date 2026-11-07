from app.modules.billing.products import READY_PRODUCTS
from app.modules.plans.catalog import (
    BUSINESS_MODEL,
    FREE_MODULE_ENTITLEMENTS,
    MODULE_PRODUCTS,
    PAID_MODULE_ENTITLEMENTS,
    free_plan_limits,
    internal_mvp_limits,
)


def test_billing_products_use_plan_catalog_prices() -> None:
    module_products = {product["code"]: product for product in MODULE_PRODUCTS if product.get("status") == "available"}
    plan_products = {product["code"]: product for product in BUSINESS_MODEL["self_serve_plans"]}

    for code, catalog_product in module_products.items():
        billing_product = READY_PRODUCTS[code]
        yearly_billing_product = READY_PRODUCTS[f"{code}.yearly"]

        assert billing_product.amount == catalog_product["monthly_price"]
        assert billing_product.yearly_amount == catalog_product["yearly_price"]
        assert yearly_billing_product.amount == catalog_product["yearly_price"]

    for code, catalog_product in plan_products.items():
        billing_product = READY_PRODUCTS[code]
        yearly_billing_product = READY_PRODUCTS[f"{code}.yearly"]

        assert billing_product.amount == catalog_product["monthly_price"]
        assert billing_product.yearly_amount == catalog_product["yearly_price"]
        assert yearly_billing_product.amount == catalog_product["yearly_price"]


def test_public_offer_has_one_primary_product_and_no_automatic_renewal() -> None:
    public_offer = BUSINESS_MODEL["public_offer"]

    assert public_offer["primary_product_code"] == "core"
    assert public_offer["primary_period_days"] == 30
    assert public_offer["implementation_pilot_price"] == 59900
    assert public_offer["automatic_renewal"] is False


def test_parser_module_prices_match_public_landing_prices() -> None:
    parser_products = [product for product in MODULE_PRODUCTS if str(product["code"]).startswith("module.parsing.")]

    assert parser_products
    assert {product["monthly_price"] for product in parser_products} == {490}
    assert {product["yearly_price"] for product in parser_products} == {4900}


def test_free_plan_excludes_paid_modules() -> None:
    free_limits = free_plan_limits()

    assert set(FREE_MODULE_ENTITLEMENTS).issubset(free_limits)
    for code in PAID_MODULE_ENTITLEMENTS:
        assert code not in free_limits


def test_content_factory_is_paid_available_module() -> None:
    product = next(product for product in MODULE_PRODUCTS if product["code"] == "module.content_factory")

    assert "module.content_factory" in PAID_MODULE_ENTITLEMENTS
    assert "module.content_factory" not in FREE_MODULE_ENTITLEMENTS
    assert product["status"] == "available"
    assert product["monthly_price"] == 6990
    assert product["yearly_price"] == 69900


def test_content_factory_access_limits() -> None:
    standalone_limits = READY_PRODUCTS["module.content_factory"].limits

    assert standalone_limits["module.content_factory"] == 1
    assert standalone_limits["module.ai"] == 1
    assert standalone_limits["module.datasets"] == 1
    assert "module.content_factory" not in READY_PRODUCTS["start"].limits
    assert READY_PRODUCTS["core"].limits["module.content_factory"] == 1
    assert READY_PRODUCTS["agency"].limits["module.content_factory"] == 1


def test_paid_plan_limits_are_account_led() -> None:
    assert READY_PRODUCTS["start"].limits["telegram_accounts"] == 5
    assert READY_PRODUCTS["core"].limits["telegram_accounts"] == 20
    assert READY_PRODUCTS["agency"].limits["telegram_accounts"] == 60
    assert READY_PRODUCTS["pro"].limits["telegram_accounts"] == 60

    assert READY_PRODUCTS["start"].limits["active_campaigns"] == 50
    assert READY_PRODUCTS["core"].limits["active_campaigns"] == 200
    assert READY_PRODUCTS["agency"].limits["active_campaigns"] == 1000
    assert READY_PRODUCTS["start"].limits["sources"] == 5000
    assert READY_PRODUCTS["core"].limits["sources"] == 25000
    assert READY_PRODUCTS["agency"].limits["sources"] == 100000


def test_internal_plan_is_unlimited_owner_access() -> None:
    limits = internal_mvp_limits()

    assert limits["telegram_accounts"] == 1_000_000
    assert limits["active_campaigns"] == 1_000_000
    assert limits["sources"] == 1_000_000
    assert limits["comments_monthly"] == 1_000_000
    assert limits["workspace_members"] == 1_000_000
    for code in PAID_MODULE_ENTITLEMENTS:
        assert limits[code] == 1
