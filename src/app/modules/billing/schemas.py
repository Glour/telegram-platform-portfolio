from pydantic import BaseModel, Field


class CheckoutCreate(BaseModel):
    product_code: str | None = Field(default=None, min_length=2, max_length=80)
    items: list[str] | None = Field(default=None, min_length=1, max_length=20)
    provider: str = Field(default="yookassa", pattern="^(yookassa|nowpayments)$")
    payment_method: str | None = Field(default=None, max_length=40)


class CheckoutResponse(BaseModel):
    order_id: str
    provider: str
    status: str
    checkout_url: str
    amount: float
    currency: str
    product_code: str
    product_name: str


class PaymentOrderResponse(BaseModel):
    id: str
    provider: str
    product_code: str
    product_name: str
    amount: float
    currency: str
    status: str
    checkout_url: str | None
    provider_payment_id: str | None


class CartUpdate(BaseModel):
    items: list[str] = Field(default_factory=list, max_length=50)
    provider: str = Field(default="yookassa", pattern="^(yookassa|nowpayments)$")


class CartItemUpdate(BaseModel):
    product_code: str = Field(min_length=2, max_length=80)


class CartCheckoutCreate(BaseModel):
    provider: str = Field(default="yookassa", pattern="^(yookassa|nowpayments)$")
    payment_method: str | None = Field(default=None, max_length=40)


class CartProductResponse(BaseModel):
    code: str
    name: str
    amount: float
    currency: str
    duration_days: int


class CartResponse(BaseModel):
    user_id: str
    workspace_id: str
    items: list[str]
    provider: str
    products: list[CartProductResponse]
    total: float
    currency: str
