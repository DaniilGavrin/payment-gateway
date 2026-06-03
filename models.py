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

class CryptoCloudWebhook(BaseModel):
    status: str = Field(..., pattern="^(success|failed|expired|canceled)$")
    invoice_id: str = Field(..., min_length=1)  # без префикса INV
    order_id: str | None = None
    amount_crypto: float = Field(..., gt=0)
    currency: str = Field(..., min_length=3)
    token: str = Field(..., min_length=20)
    invoice_info: dict | None = None  # подробная инфа, если пришла


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

    telegram_id: Optional[str] = Field(None, max_length=64, description="ID пользователя Telegram")
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
        # Опциональная проверка: сумма позиций ≈ total (с допуском на копеечные расхождения)
        if 'total_rub' in values.data:
            calc_total = sum(item.price_rub for item in v)
            if abs(calc_total - values.data['total_rub']) > 1:  # допуск 1 рубль
                raise ValueError(f"Сумма позиций ({calc_total}) не совпадает с total_rub")
        return v

# --- Ответ фронтенду ---
class OrderCreateOut(BaseModel):
    success: bool
    payment_url: Optional[str] = None
    order_id: str
    message: Optional[str] = None