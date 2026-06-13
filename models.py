from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from typing import List, Optional, Dict, Any

class PaymentRequest(BaseModel):
    amount: float = Field(..., ge=1, le=100000, description="Сумма платежа")
    currency: str = Field(default="USD", pattern="^(USD|RUB|EUR|USDT)$")
    order_id: str = Field(..., min_length=1, max_length=64, description="ID заказа в твоей системе")

class PaymentCancel(BaseModel):
    uuid: str = Field(description="UUID счёта")

class PaymentList(BaseModel):
    start: str = Field(..., description="Дата начала в формате dd.mm.yyyy", examples=["01.01.2026"])
    end: str = Field(..., description="Дата конца в формате dd.mm.yyyy", examples=["31.01.2026"])
    offset: int = Field(default=0, ge=0, description="Номер начальной записи")
    limit: int = Field(default=10, ge=1, le=100, description="Количество записей (1-100)")

    # 🔹 Валидация формата даты
    @field_validator("start", "end")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        try:
            datetime.strptime(v, "%d.%m.%Y")
        except ValueError:
            raise ValueError("Дата должна быть в формате dd.mm.yyyy")
        return v

    # 🔹 Валидация: end >= start
    @model_validator(mode="after")
    def validate_date_range(self):
        start_dt = datetime.strptime(self.start, "%d.%m.%Y")
        end_dt = datetime.strptime(self.end, "%d.%m.%Y")
        if end_dt < start_dt:
            raise ValueError("Дата end должна быть >= start")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "start": "01.01.2026",
                "end": "31.01.2026",
                "offset": 0,
                "limit": 10
            }]
        }
    }

class NOWPaymentsWebhook(BaseModel):
    """
    Модель IPN-уведомления от NOWPayments.
    Документация: https://docs.nowpayments.io/ipn-notifications/
    """
    payment_id: int = Field(..., description="ID платежа в NOWPayments")
    payment_status: str = Field(..., description="Статус: waiting/confirming/confirmed/sending/finished/failed/refunded/expired")
    pay_address: str = Field(..., description="Адрес кошелька для оплаты")
    price_amount: float = Field(..., gt=0, description="Цена в price_currency")
    price_currency: str = Field(..., description="Валюта цены (usd/rub)")
    pay_amount: float = Field(..., description="Сумма к оплате в pay_currency")
    actually_paid: float = Field(default=0, description="Фактически оплачено")
    pay_currency: str = Field(..., description="Валюта оплаты (trc20usdt/ton/btc)")
    order_id: str = Field(..., min_length=1, description="Наш order_code")
    order_description: Optional[str] = Field(None, max_length=500)
    purchase_id: Optional[str] = None
    outcome_amount: Optional[float] = None
    outcome_currency: Optional[str] = None
    created_at: str = Field(..., description="ISO datetime")
    updated_at: Optional[str] = None
    outcome_wallet_address: Optional[str] = None
    network: Optional[str] = None
    pay_extra: Optional[Any] = None
    status: Optional[str] = None  # дублирует payment_status в некоторых версиях API
    signature: Optional[str] = Field(None, description="HMAC SHA512 подпись от NOWPayments")


# --- Вспомогательные модели для конфигурации товара ---
class OrderItemConfig(BaseModel):
    """Слепок всех выборов пользователя в конфигураторе"""
    class Config:
        extra = "allow"  # Разрешаем любые доп. поля из config_schema

# --- Элемент заказа (товар с конфигом) ---
class OrderItemIn(BaseModel):
    product_id: int = Field(..., ge=1)
    product_name: str = Field(..., min_length=1, max_length=255)
    config: OrderItemConfig  # 🔥 Все галочки, селекты, ТЗ
    price_rub: float = Field(..., ge=0)
    delivery_days: int = Field(..., ge=1)

# --- Основной payload создания заказа ---
class OrderCreateIn(BaseModel):
    order_id: str = Field(..., min_length=5, max_length=32, description="Код заказа вида ord_...")
    items: List[OrderItemIn] = Field(..., min_length=1)
    total_rub: float = Field(..., ge=0)
    contact_email: str = Field(..., pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    contact_phone: Optional[str] = Field(None, max_length=32)
    client_comment: Optional[str] = Field(None, max_length=1000)
    payment_method: str = Field(..., pattern="^(card|sbp|crypto|invoice|stars)$")
    locale: str = Field(default="ru", pattern="^(ru|en)$")

    # ✅ telegram_id теперь опциональный (берём из JWT, но фронт может передать для обратной совместимости)
    telegram_id: Optional[str] = Field(None, max_length=64, description="ID пользователя Telegram (опционально, берётся из JWT)")
    telegram_username: Optional[str] = Field(None, max_length=64, description="Username в Telegram")
    telegram_first_name: Optional[str] = Field(None, max_length=128, description="Имя в Telegram")
    telegram_last_name: Optional[str] = Field(None, max_length=128, description="Фамилия в Telegram")

    company_name: Optional[str] = Field(None, max_length=255, description="Название организации")
    company_inn: Optional[str] = Field(None, max_length=12, description="ИНН организации")
    company_kpp: Optional[str] = Field(None, max_length=9, description="КПП организации")
    company_legal_address: Optional[str] = Field(None, max_length=500, description="Юридический адрес")

    @field_validator('items')
    @classmethod
    def validate_items_total(cls, v, values):
        if 'total_rub' in values.data:
            calc_total = sum(item.price_rub for item in v)
            if abs(calc_total - values.data['total_rub']) > 1:
                raise ValueError(f"Сумма позиций ({calc_total}) не совпадает с total_rub")
        return v

# --- Ответ фронтенду ---
class OrderCreateOut(BaseModel):
    success: bool
    payment_url: Optional[str] = None
    order_id: str
    message: Optional[str] = None