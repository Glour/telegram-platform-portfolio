from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.modules.plans.catalog import BUSINESS_MODEL, MODULE_PRODUCTS, READY_PAID_MODULE_CODES


@dataclass(frozen=True)
class BillingProduct:
    code: str
    name: str
    amount: int
    currency: str
    limits: dict[str, int]
    plan_name: str
    duration_days: int = 30
    yearly_amount: int = 0


BASE_PAID_LIMITS = {
    "module.channel_base": 1,
    "telegram_accounts": 5,
    "active_campaigns": 50,
    "sources": 5000,
}

READY_MODULE_LIMITS = {code: 1 for code in READY_PAID_MODULE_CODES}

MODULE_PRODUCT_LIMITS = {
    "module.neurocommenting": {
        **BASE_PAID_LIMITS,
        "module.neurocommenting": 1,
        "module.ai": 1,
        "module.datasets": 1,
    },
    "module.message_broadcast": {**BASE_PAID_LIMITS, "module.message_broadcast": 1},
    "module.warmup": {**BASE_PAID_LIMITS, "module.warmup": 1},
    "module.content_factory": {
        **BASE_PAID_LIMITS,
        "module.content_factory": 1,
        "module.ai": 1,
        "module.datasets": 1,
    },
}

for _module_code in READY_PAID_MODULE_CODES:
    MODULE_PRODUCT_LIMITS.setdefault(_module_code, {**BASE_PAID_LIMITS, _module_code: 1})

PLAN_PRODUCT_LIMITS = {
    "start": {
        "module.neurocommenting": 1,
        "module.message_broadcast": 1,
        "module.channel_base": 1,
        "module.warmup": 1,
        "module.ai": 1,
        "module.datasets": 1,
        "telegram_accounts": 5,
        "active_campaigns": 50,
        "sources": 5000,
    },
    "core": {
        **READY_MODULE_LIMITS,
        "telegram_accounts": 20,
        "active_campaigns": 200,
        "sources": 25000,
        "workspace_members": 2,
    },
    "pro": {
        **READY_MODULE_LIMITS,
        "telegram_accounts": 60,
        "active_campaigns": 1000,
        "sources": 100000,
        "workspace_members": 5,
    },
    "agency": {
        **READY_MODULE_LIMITS,
        "telegram_accounts": 60,
        "active_campaigns": 1000,
        "sources": 100000,
        "workspace_members": 5,
    },
}


def _catalog_module_product(code: str) -> dict:
    for product in MODULE_PRODUCTS:
        if product.get("code") == code:
            return product
    raise KeyError(f"Module product {code} is missing from plans catalog")


def _catalog_plan_product(code: str) -> dict:
    for plan in BUSINESS_MODEL.get("self_serve_plans", []):
        if plan.get("code") == code:
            return plan
    if code == "pro":
        agency = _catalog_plan_product("agency")
        return {**agency, "code": "pro", "name": "Pro"}
    raise KeyError(f"Plan product {code} is missing from business model")


def _module_billing_product(code: str, *, yearly: bool = False) -> BillingProduct:
    product = _catalog_module_product(code)
    amount = int(product["yearly_price"] if yearly else product["monthly_price"])
    return BillingProduct(
        code=f"{code}.yearly" if yearly else code,
        name=f"{product['name']} на год" if yearly else str(product["name"]),
        amount=amount,
        yearly_amount=0 if yearly else int(product.get("yearly_price") or 0),
        currency=str(BUSINESS_MODEL.get("currency") or "RUB"),
        plan_name=str(product["name"]),
        duration_days=365 if yearly else 30,
        limits=MODULE_PRODUCT_LIMITS[code],
    )


def _plan_billing_product(code: str, *, yearly: bool = False) -> BillingProduct:
    product = _catalog_plan_product(code)
    amount = int(product["yearly_price"] if yearly else product["monthly_price"])
    return BillingProduct(
        code=f"{code}.yearly" if yearly else code,
        name=f"{product['name']} на год" if yearly else str(product["name"]),
        amount=amount,
        yearly_amount=0 if yearly else int(product.get("yearly_price") or 0),
        currency=str(BUSINESS_MODEL.get("currency") or "RUB"),
        plan_name=str(product["name"]),
        duration_days=365 if yearly else 30,
        limits=PLAN_PRODUCT_LIMITS[code],
    )


READY_PRODUCTS: dict[str, BillingProduct] = {}
for _code in [str(product["code"]) for product in MODULE_PRODUCTS if product.get("status") == "available"]:
    READY_PRODUCTS[_code] = _module_billing_product(_code)
    READY_PRODUCTS[f"{_code}.yearly"] = _module_billing_product(_code, yearly=True)
for _code in ("start", "core", "pro", "agency"):
    READY_PRODUCTS[_code] = _plan_billing_product(_code)
    READY_PRODUCTS[f"{_code}.yearly"] = _plan_billing_product(_code, yearly=True)

PLAN_RANK = {"start": 1, "core": 2, "pro": 3, "agency": 3}
PLAN_INCLUDED_CODES = {
    "start": {
        "module.neurocommenting",
        "module.message_broadcast",
        "module.warmup",
        "module.channel_base",
        "module.ai",
        "module.datasets",
    },
    "core": set(READY_MODULE_LIMITS),
    "pro": set(READY_MODULE_LIMITS),
    "agency": set(READY_MODULE_LIMITS),
}


def base_product_code(code: str) -> str:
    return code.removesuffix(".yearly")


def normalize_product_codes(codes: list[str]) -> list[str]:
    selected: list[str] = []
    by_base: dict[str, str] = {}
    best_plan: str | None = None

    for raw_code in codes:
        code = str(raw_code or "").strip()
        product = get_billing_product(code)
        if not code or not product:
            continue
        base = base_product_code(code)
        if base in PLAN_RANK:
            current_base = base_product_code(best_plan) if best_plan else None
            if not current_base or PLAN_RANK[base] >= PLAN_RANK[current_base]:
                best_plan = code
            continue
        by_base[base] = code

    if best_plan:
        plan_base = base_product_code(best_plan)
        included = PLAN_INCLUDED_CODES.get(plan_base, set())
        selected.append(best_plan)
        for base, code in by_base.items():
            if base not in included:
                selected.append(code)
    else:
        selected.extend(by_base.values())

    return selected


def get_billing_product(code: str) -> BillingProduct | None:
    return READY_PRODUCTS.get(code)


def product_to_dict(product: BillingProduct) -> dict[str, object]:
    return {
        "code": product.code,
        "name": product.name,
        "amount": product.amount,
        "currency": product.currency,
        "limits": product.limits,
        "plan_name": product.plan_name,
        "duration_days": product.duration_days,
        "yearly_amount": product.yearly_amount,
    }


def product_from_dict(data: dict | None) -> BillingProduct | None:
    if not isinstance(data, dict):
        return None
    try:
        limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
        return BillingProduct(
            code=str(data["code"]),
            name=str(data["name"]),
            amount=int(data["amount"]),
            currency=str(data.get("currency") or "RUB"),
            limits={str(key): int(value) for key, value in limits.items()},
            plan_name=str(data.get("plan_name") or data["name"]),
            duration_days=int(data.get("duration_days") or 30),
            yearly_amount=int(data.get("yearly_amount") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_cart_product(codes: list[str]) -> BillingProduct:
    unique_codes = normalize_product_codes(codes)
    if not unique_codes:
        raise ValueError("Корзина пустая.")
    products: list[BillingProduct] = []
    for code in unique_codes:
        product = get_billing_product(code)
        if not product:
            raise ValueError("Один из товаров пока недоступен для оплаты.")
        products.append(product)
    if len(products) == 1:
        return products[0]

    limits: dict[str, int] = {}
    for product in products:
        for feature_code, limit_value in product.limits.items():
            limits[feature_code] = max(int(limit_value), limits.get(feature_code, 0))
    limits["module.channel_base"] = 1
    amount = sum(product.amount for product in products)
    duration_days = min(product.duration_days for product in products)
    digest = hashlib.sha1("|".join(unique_codes).encode("utf-8")).hexdigest()[:12]
    names = ", ".join(product.name for product in products)
    return BillingProduct(
        code=f"cart.{digest}",
        name=f"Корзина: {names}",
        amount=amount,
        currency="RUB",
        limits=limits,
        plan_name="Индивидуальный набор",
        duration_days=duration_days,
    )
