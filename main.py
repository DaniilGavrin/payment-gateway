import asyncio
from io import BytesIO
import os
from fastapi.params import Path, Query
import requests
import jwt
import logging
import json
import time
import hashlib
import hmac
import http.client
import generator
import httpx

from typing import Any
from urllib.parse import parse_qsl
from datetime import datetime
from contextlib import asynccontextmanager
from database.db import db
from dotenv import load_dotenv
from dependencies import require_db_connection

from models import PaymentRequest, PaymentCancel, PaymentList, CryptoCloudWebhook, OrderCreateIn, OrderCreateOut, OrderItemConfig, OrderItemIn
from services.webhook_service import is_webhook_processed, process_payment_webhook, mark_webhook_processed

from fastapi import FastAPI, HTTPException, Request, Depends, Header, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse, FileResponse, Response

from services.email_service import EmailService
from services.invoice_pdf_service import InvoicePDFService
from auth.dependencies import get_current_user

email_service = EmailService()
invoice_pdf_service = InvoicePDFService()

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET")

COMPANY_REQUISITES = {
    "name": os.getenv("COMPANY_NAME", "ИП Гаврин Даниил Никитич"),
    "inn": os.getenv("COMPANY_INN", "434584462396"),
    "ogrn": os.getenv("COMPANY_OGRN"),
    "address": os.getenv("COMPANY_ADDRESS"),
    "email": os.getenv("COMPANY_EMAIL"),
    "phone": os.getenv("COMPANY_PHONE"),
    "director": os.getenv("COMPANY_DIRECTOR", "Гаврин Д.Н."),
    "accountant": os.getenv("COMPANY_ACCOUNTANT"),
    "bank_name": os.getenv("COMPANY_BANK_NAME", "ПАО Сбербанк"),
    "bank_account": os.getenv("COMPANY_BANK_ACCOUNT"),
    "bank_bik": os.getenv("COMPANY_BANK_BIK"),
    "bank_corr": os.getenv("COMPANY_BANK_CORR"),
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    yield
    await db.disconnect()

app = FastAPI(lifespan=lifespan)
logger = logging.getLogger(__name__)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://shop.bytewizard.ru",],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verify_cryptocloud_jwt(token: str) -> bool:
    """Проверяет JWT-токен от CryptoCloud (HS256, SECRET_KEY)."""
    secret = os.getenv("CRYPTOCLOUD_SECRET_KEY", "").encode()
    if not secret:
        logger.warning("⚠️ CRYPTOCLOUD_SECRET_KEY not set")
        return False

    try:
        jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp"]})
        return True
    except jwt.ExpiredSignatureError:
        logger.warning(f"⚠️ JWT expired: {token[:20]}...")
        return False
    except jwt.InvalidSignatureError:
        logger.error(f"❌ JWT signature mismatch: {token[:20]}...")
        return False
    except jwt.DecodeError as e:
        logger.error(f"❌ JWT decode error: {e}")
        return False

def verify_admin_token(token: str = Header(..., alias="X-Admin-Token")):
    expected = os.getenv("CRYPTOCLOUD_SECRET")
    if not expected or token != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token")
    return token

def calculate_server_price(
    base_price: float,
    selections: dict[str, Any],
    config_schema: dict[str, Any]
) -> float:
    """
    Серверная версия calculateDynamicPrice из фронтенда.
    Должна быть ИДЕНТИЧНА клиентской логике.
    """
    total = float(base_price)
    
    for field_id, field in config_schema.items():
        val = selections.get(field_id)
        if val is None:
            continue
        
        field_type = field.get("type")
        
        if field_type == "checkbox" and val is True:
            total += float(field.get("price_modifier", 0))
            
        elif field_type == "select" and isinstance(val, str):
            options = field.get("options", [])
            opt = next((o for o in options if o["value"] == val), None)
            if opt:
                total += float(opt.get("price_modifier", 0))
                
        elif field_type == "number" and isinstance(val, (int, float)):
            price_per_unit = float(field.get("price_per_unit", 0))
            if price_per_unit > 0:
                base_count = float(field.get("default", field.get("min", 0)))
                extra = max(0, val - base_count)
                total += extra * price_per_unit
                
        elif field_type == "multiselect" and isinstance(val, list):
            options = field.get("options", [])
            for v in val:
                opt = next((o for o in options if o["value"] == v), None)
                if opt:
                    total += float(opt.get("price_modifier", 0))
    
    return round(total, 2)


def calculate_server_delivery_days(
    base_days: int,
    selections: dict[str, Any],
    config_schema: dict[str, Any],
    delivery_meta: dict[str, Any]
) -> int:
    """
    Серверная версия calculateDynamicDelivery из фронтенда.
    """
    option_days = 0
    
    for field_id, field in config_schema.items():
        if field_id == "urgency":
            continue
            
        val = selections.get(field_id)
        if val is None:
            continue
        
        def get_days(mod=None, price=None):
            if mod is not None:
                return mod
            return max(0, int((price or 0) / 8000))
        
        field_type = field.get("type")
        
        if field_type == "checkbox" and val is True:
            option_days += get_days(
                field.get("delivery_days_modifier"),
                field.get("price_modifier")
            )
        elif field_type == "select" and isinstance(val, str):
            options = field.get("options", [])
            opt = next((o for o in options if o["value"] == val), None)
            if opt:
                option_days += get_days(
                    opt.get("delivery_days_modifier"),
                    opt.get("price_modifier")
                )
        elif field_type == "multiselect" and isinstance(val, list):
            options = field.get("options", [])
            for v in val:
                opt = next((o for o in options if o["value"] == v), None)
                if opt:
                    option_days += get_days(
                        opt.get("delivery_days_modifier"),
                        opt.get("price_modifier")
                    )
    
    total_days = base_days + option_days
    
    # Применяем множитель срочности
    urgency = selections.get("urgency")
    URGENCY_MULTIPLIERS = {
        "normal": 1.0,
        "fast": 0.85,
        "ultra_fast": 0.70,
    }
    multiplier = URGENCY_MULTIPLIERS.get(urgency or "normal", 1.0)
    total_days = int(total_days * multiplier)
    
    min_days = delivery_meta.get("min_days") or int(base_days * 0.4)
    return max(min_days, int(total_days) + (1 if total_days % 1 > 0 else 0))

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "FastAPI Payment Gateway is running"
    }

@app.post("/orders/create", response_model=OrderCreateOut)
async def create_order(
    payload: OrderCreateIn,
    current_user: dict = Depends(get_current_user),
    _db: bool = Depends(require_db_connection)
):
    logger.info(f"📦 Новый заказ: {payload.order_id} | User: {current_user['tg_id']}")
    
    try:
        # 🔥 ШАГ 0: ВАЛИДАЦИЯ ЦЕНЫ И СРОКОВ НА СЕРВЕРЕ
        server_calculated_total = 0.0
        is_price_suspicious = False
        client_submitted_total = payload.total_rub
        
        for item in payload.items:
            # 1. Получаем реальные данные товара из БД
            product = await db.fetchrow(
                "SELECT base_price_rub, metadata, is_active FROM products WHERE id = $1", 
                item.product_id
            )
            
            if not product or not product["is_active"]:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Товар с ID {item.product_id} не найден или неактивен"
                )
            
            # 2. Парсим metadata (если это строка)
            metadata = product["metadata"]
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            
            config_schema = metadata.get("config_schema", {})
            delivery_meta = metadata.get("delivery", {})
            
            # 3. Пересчитываем цену на сервере
            calculated_price = calculate_server_price(
                product["base_price_rub"],
                item.config.dict(),
                config_schema
            )
            
            # 4. Пересчитываем сроки доставки
            calculated_delivery = calculate_server_delivery_days(
                delivery_meta.get("base_days", 1),
                item.config.dict(),
                config_schema,
                delivery_meta
            )
            
            # 5. Сравниваем с присланными значениями
            price_diff = abs(calculated_price - item.price_rub)
            delivery_diff = abs(calculated_delivery - item.delivery_days)
            
            if price_diff > 1.0 or delivery_diff > 1:
                is_price_suspicious = True
                logger.warning(
                    f"🚨 ПОДОЗРИТЕЛЬНЫЙ ТОВАР! Product: {item.product_id} | "
                    f"Клиент указал: {item.price_rub}₽ / {item.delivery_days} дн. | "
                    f"Сервер насчитал: {calculated_price}₽ / {calculated_delivery} дн."
                )
            
            server_calculated_total += calculated_price
        
        # 6. Финальная проверка общей суммы
        total_diff = abs(server_calculated_total - client_submitted_total)
        if total_diff > 1.0:
            is_price_suspicious = True
            logger.warning(
                f"🚨 ПОДМЕНА ИТОГОВОЙ СУММЫ! Order: {payload.order_id} | "
                f"Клиент: {client_submitted_total}₽ | Сервер: {server_calculated_total}₽ | "
                f"Разница: {total_diff}₽"
            )
        
        if not is_price_suspicious:
            logger.info(f"✅ Валидация пройдена. Итого: {server_calculated_total}₽")
        
        # 🔹 ШАГ 1: Сохраняем заказ в БД (с реальной ценой)
        tg_id_val = current_user["tg_id"]
        username_val = current_user.get("username")
        first_name_val = payload.telegram_first_name or "Клиент"

        await db.execute(
            """
            INSERT INTO orders (
                order_code, client_email, client_phone, payment_method, 
                total_rub, status, 
                tg_id, telegram_username, telegram_first_name, telegram_last_name,
                client_comment, is_price_suspicious, client_submitted_total,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, NOW(), NOW())
            """,
            payload.order_id,
            payload.contact_email,
            payload.contact_phone,
            payload.payment_method,
            server_calculated_total,  # 🔥 Реальная цена
            'pending',
            tg_id_val,                      
            username_val,      
            first_name_val,    
            payload.telegram_last_name,
            payload.client_comment,
            is_price_suspicious,  # 🔥 Флаг подозрительности
            client_submitted_total  # 🔥 Цена, которую прислал клиент
        )
        
        # 🔹 ШАГ 2: Сохраняем позиции заказа
        for item in payload.items:
            await db.execute(
                """
                INSERT INTO order_items (
                    order_id, order_code, product_id, product_name,
                    config, price_rub, delivery_days, created_at
                ) VALUES (
                    (SELECT id FROM orders WHERE order_code = $1),
                    $1, $2, $3, $4, $5, $6, NOW()
                )
                """,
                payload.order_id,
                item.product_id,
                item.product_name,
                json.dumps(item.config.dict()),
                item.price_rub,
                item.delivery_days
            )
        
        # 🔹 3. Маршрутизация на платежный шлюз
        payment_url = None
        
        if payload.payment_method in ['card', 'sbp']:
            payment_url = await _create_tbank_payment(payload)
            
        elif payload.payment_method == 'crypto':
            payment_url = await _create_cryptocloud_payment(payload)

        elif payload.payment_method == 'stars':
            payment_url = await _create_telegram_stars_payment(payload)
            
        elif payload.payment_method == 'invoice':
            # 📄 Генерация PDF и отправка в TG + email
            payment_url = await _generate_and_send_invoice(payload)
        
        if not payment_url:
            raise HTTPException(status_code=502, detail="Не удалось создать ссылку на оплату")
        
        logger.info(f"✅ Заказ {payload.order_id} создан. Оплата: {payment_url}")

        logger.info("📤 Пытаемся отправить уведомление в Telegram...")
        logger.info(f"   Order ID: {payload.order_id}")
        logger.info(f"   Total: {payload.total_rub}")
        logger.info(f"   Email: {payload.contact_email}")
        
        try:
            logger.info("📤 Начинаем отправку уведомлений в Telegram...")
            await send_telegram_notifications(
                order_id=payload.order_id,
                amount_rub=server_calculated_total,
                event="created",
                is_price_suspicious=is_price_suspicious,
                client_submitted_total=client_submitted_total,
                payment_url=payment_url,
                telegram_id=payload.telegram_id,
                telegram_username=payload.telegram_username,
                telegram_first_name=payload.telegram_first_name,
                email=payload.contact_email,
                phone=payload.contact_phone,
                method=payload.payment_method,
                comment=payload.client_comment
            )
            logger.info("✅ Уведомления в Telegram успешно обработаны")
        except Exception as e:
            logger.error(f"⚠️ Ошибка при отправке уведомлений: {e}", exc_info=True)
        
        return OrderCreateOut(
            success=True,
            payment_url=payment_url,
            order_id=payload.order_id,
            message="Order created successfully"
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка создания заказа {payload.order_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error while creating order")

async def _create_tbank_payment(order: OrderCreateIn) -> str:
    """Создает платеж в T-Bank и возвращает ссылку на оплату"""
    amount_kopecks = int(order.total_rub * 100)

    payment_params = {
        "TerminalKey": os.getenv("TERMINAL_KEY"),
        "Amount": amount_kopecks,
        "OrderId": order.order_id,
        "Description": f"Заказ в ByteWizard: {order.items[0].product_name}",
        "CustomerKey": order.contact_email or "guest@bytewizard.ru",
        "PayType": "O",
        "Language": order.locale,
    }

    token = generator.generate_tinkoff_token(payment_params, os.getenv("SECRET_PASSWORD"))

    receipt = {
        "Email": order.contact_email or "test@test.com",
        "Phone": order.contact_phone or "",
        "Taxation": "usn_income",
        "Items": [
            {
                "Name": order.items[0].product_name,
                "Price": amount_kopecks,
                "Quantity": 1.0,
                "Amount": amount_kopecks,
                "Tax": "none" 
            }
        ]
    }

    full_payload = {
        **payment_params,
        "Token": token,
        "Receipt": receipt,
        "DATA": {
            "Email": order.contact_email or "",
            "Phone": order.contact_phone or "",
            "OperationInitiatorType": "0"
        },
    }

    conn = http.client.HTTPSConnection(os.getenv("TINKOFF_API_URL"))
    headers = {"Content-Type": "application/json; charset=utf-8"}

    try:
        body = json.dumps(full_payload, ensure_ascii=False).encode('utf-8')
        conn.request("POST", "/v2/Init", body=body, headers=headers)
        response = conn.getresponse()
        result = json.loads(response.read().decode("utf-8"))

        if result.get("Success"):
            return result["PaymentURL"]
        else:
            logger.error(f"T-Bank error: {result}")
            raise Exception(f"T-Bank API error: {result.get('Message')} | Code: {result.get('ErrorCode')}")

    finally:
        conn.close()

async def _create_cryptocloud_payment(order: OrderCreateIn) -> str:
    """Создает инвойс в CryptoCloud и возвращает ссылку"""
    url = f"{os.getenv('CRYPTOCLOUD_API_URL')}invoice/create"
    headers = {
        "Authorization": f"Token {os.getenv('CRYPTOCLOUD_API_KEY')}",
        "Content-Type": "application/json"
    }
    
    data = {
        "amount": order.total_rub,
        "shop_id": os.getenv("CRYPTOCLOUD_SHOP_ID"),
        "currency": "RUB",
        "order_id": order.order_id,
        "success_url": f"https://shop.bytewizard.ru/{order.locale}/profile?order={order.order_id}&status=success",
        "fail_url": f"https://shop.bytewizard.ru/{order.locale}/profile?order={order.order_id}&status=failed",
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        result = response.json()["result"]
        return result["link"]
    else:
        logger.error(f"CryptoCloud error: {response.text}")
        raise Exception(f"CryptoCloud API error: {response.text[:200]}")

async def _create_telegram_stars_payment(order: OrderCreateIn) -> str:
    """Создает инвойс в Telegram Stars и возвращает прямую ссылку на оплату"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise Exception("TELEGRAM_BOT_TOKEN не задан в .env")
    
    stars_amount = max(1, int(order.total_rub / 2)) 
    
    url = f"https://api.telegram.org/bot{bot_token}/createInvoiceLink"
    payload = {
        "title": f"Заказ в ByteWizard #{order.order_id}",
        "description": f"Оплата заказа на сумму {order.total_rub}₽",
        "payload": order.order_id,  
        "provider_token": "",       
        "currency": "XTR",          
        "prices": [{"label": "Стоимость", "amount": stars_amount}]
    }
    
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload)
        result = response.json()
        
    if result.get("ok"):
        return result["result"] 
    else:
        logger.error(f"Telegram Stars error: {result}")
        raise Exception(f"Telegram API error: {result.get('description')}")

async def _generate_and_send_invoice(order: OrderCreateIn) -> str:
    """
    Генерирует PDF, отправляет в Telegram и на email.
    Возвращает URL для редиректа фронта (просто ссылка на профиль).
    """
    try:
        # 1. Генерируем номер счёта
        invoice_number = await db.fetchval(
            "SELECT COALESCE(MAX(invoice_number), 'INV-0000') FROM invoices"
        )
        prefix = "INV-"
        num = int(invoice_number.split("-")[1]) + 1
        invoice_number = f"{prefix}{num:04d}"
        
        # 2. Данные покупателя
        buyer_data = {
            "first_name": order.telegram_first_name,
            "last_name": order.telegram_last_name,
            "email": order.contact_email,
            "phone": order.contact_phone,
            "company_name": order.company_name,
            "inn": order.company_inn,
            "kpp": order.company_kpp,
            "legal_address": order.company_legal_address,
        }
        
        # 3. Генерируем PDF
        pdf_bytes = invoice_pdf_service.generate_invoice_pdf(
            invoice_number=invoice_number,
            order={
                "items": [item.dict() for item in order.items],
                "total_rub": order.total_rub,
            },
            seller=COMPANY_REQUISITES,
            buyer=buyer_data
        )
        
        # 4. Сохраняем данные в БД
        await db.execute(
            """
            INSERT INTO invoices (
                invoice_number, order_code, buyer_data, seller_data, 
                items_data, total_rub, status
            ) VALUES ($1, $2, $3, $4, $5, $6, 'sent')
            """,
            invoice_number,
            order.order_id,
            json.dumps(buyer_data),
            json.dumps(COMPANY_REQUISITES),
            json.dumps([item.dict() for item in order.items]),
            order.total_rub
        )
        
        # 5. Отправляем PDF в Telegram
        await send_invoice_to_telegram(
            invoice_number=invoice_number,
            pdf_bytes=pdf_bytes,
            telegram_id=order.telegram_id,
            total_rub=order.total_rub
        )
        
        # 6. Отправляем PDF на email
        await email_service.send_invoice_email(
            to_email=order.contact_email,
            invoice_number=invoice_number,
            pdf_bytes=pdf_bytes,
            total_rub=order.total_rub
        )
        
        logger.info(f"✅ Счёт {invoice_number} отправлен в TG и на email")
        
        # 7. Возвращаем ссылку на профиль (фронт покажет экран "отправлено")
        return f"/profile?order={order.order_id}&status=invoice_sent"
        
    except Exception as e:
        logger.error(f"❌ Ошибка создания счёта: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось создать счёт")

@app.get("/orders/check")
async def check_orders():
    pass

@app.post("/api/crypto-cloud/list-payments")
async def cryptocloud_list_payments(req: PaymentList, _: str = Depends(verify_admin_token), _db: bool = Depends(require_db_connection)):
    url = f"{os.getenv('CRYPTOCLOUD_API_URL')}invoice/merchant/list"

    headers = {
        "Authorization": f"Token {os.getenv('CRYPTOCLOUD_API_KEY')}",
        "Content-Type": "application/json"
    }

    data = {
        "start": req.start,
        "end": req.end,
        "offset": req.offset,
        "limit": req.limit
    }

    print(f"🔍 URL: {url}")
    print(f"🔍 Data: {data}")

    response = requests.post(url, headers=headers, json=data)

    if response.status_code == 200:
        print("Success:", response.json())
        result = response.json()
        return {
            "success": True,
            "data": result.get("result", []),
            "pagination": {
                "offset": req.offset,
                "limit": req.limit,
                "total": len(result.get("result", []))
            }
        }
    else:
        print(f"❌ Fail {response.status_code}: {response.text[:300]}")
        raise HTTPException(
            status_code=response.status_code,
            detail=f"CryptoCloud error: {response.text[:200]}"
        )

@app.post("/api/crypto-cloud/callback")
async def cryptocloud_webhook(request: Request, _db: bool = Depends(require_db_connection)):
    """
    Обработчик POSTBACK от CryptoCloud (только JSON).
    """

    if not request.headers.get("Content-Type", "").startswith("application/json"):
        logger.warning(f"❌ Wrong Content-Type: {request.headers.get('Content-Type')}")
        raise HTTPException(status_code=415, detail="Only application/json accepted")

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"❌ JSON parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        webhook = CryptoCloudWebhook(**body)
    except Exception as e:
        logger.warning(f"⚠️ Validation error: {e}")
        raise HTTPException(status_code=422, detail=f"Invalid payload: {str(e)}")

    if not verify_cryptocloud_jwt(webhook.token):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    if await is_webhook_processed(webhook.invoice_id):
        logger.info(f"⏭️ Duplicate webhook, skipping: {webhook.invoice_id}")
        return JSONResponse(status_code=200, content={"status": "ok", "skipped": True})

    await process_payment_webhook(
        invoice_id=webhook.invoice_id,
        order_id=webhook.order_id,
        status=webhook.status,
        invoice_info=webhook.invoice_info
    )

    await mark_webhook_processed(webhook.invoice_id)

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "invoice_id": webhook.invoice_id,
            "processed_at": datetime.utcnow().isoformat()
        }
    )

@app.post("/api/t-bank/notification")
async def tbank_notification(request: Request, _: bool = Depends(require_db_connection)):
    """
    Продакшен-обработчик вебхуков от Т-Банка.
    """
    try:
        data = await request.json()
        
        logger.info(f"🔔 Webhook от Т-Банка: {json.dumps(data, ensure_ascii=False)}")

        secret = os.getenv("SECRET_PASSWORD")
        logger.info(f"🔑 SECRET_PASSWORD из .env: '{secret}' (длина: {len(secret) if secret else 0})")
        
        # Проверяем токен
        is_valid = generator.verify_tbank_webhook_token(data, secret)
        logger.info(f"✅ Токен валиден: {is_valid}")
        
        if not is_valid:
            # 🔥 ПОКАЖЕМ, ЧТО МЫ СЧИТАЕМ
            data_copy = {k: v for k, v in data.items() if k != "Token"}
            data_copy["Password"] = secret
            sorted_values = ""
            for key in sorted(data_copy.keys()):
                value = data_copy[key]
                sorted_values += str(value) if value is not None else ""
            
            calculated = hashlib.sha256(sorted_values.encode('utf-8')).hexdigest()
            logger.info(f" Сортированная строка: '{sorted_values}'")
            logger.info(f"📊 Мы насчитали: {calculated}")
            logger.info(f"📊 Т-Банк прислал: {data.get('Token')}")
            
            logger.error(f"❌ НЕВЕРНЫЙ ТОКЕН!")
            return {"Status": "ERROR"}
        
        payment_id = data.get("PaymentId")
        order_id = data.get("OrderId")
        status = data.get("Status")
        amount = data.get("Amount")
        
        if not generator.verify_tbank_webhook_token(data, os.getenv("SECRET_PASSWORD")):
            logger.error(f"❌ НЕВЕРНЫЙ ТОКЕН от Т-Банка! PaymentId={payment_id}")
            return {"Status": "ERROR"}
        
        exists = await db.fetchval(
            "SELECT 1 FROM processed_webhooks WHERE payment_id = $1",
            payment_id
        )
        if exists:
            logger.info(f"⏭️ Webhook уже обработан: PaymentId={payment_id}")
            return {"Status": "OK"}
        
        new_status = "pending"
        
        if status == "CONFIRMED":
            new_status = "paid"
            logger.info(f"✅ Заказ {order_id} ОПЛАЧЕН на сумму {amount} коп.")
            
        elif status == "REJECTED":
            new_status = "failed"
            logger.warning(f"❌ Заказ {order_id} ОТКЛОНЁН банком")
            
        elif status == "REVERSED":
            new_status = "refunded"
            logger.warning(f"↩️ Заказ {order_id} ОТМЕНЁН (возврат)")
            
        elif status == "3DS_CHECKED":
            logger.info(f"⏳ Заказ {order_id}: 3D-Secure проверен, ждём CONFIRMED")
            await db.execute(
                "UPDATE orders SET status = '3ds_checked', updated_at = NOW() WHERE order_code = $1",
                order_id
            )
            await db.execute(
                "INSERT INTO processed_webhooks (payment_id) VALUES ($1)",
                payment_id
            )
            return {"Status": "OK"}
            
        else:
            logger.warning(f"⚠️ Неизвестный статус {status} для заказа {order_id}")
        
        if new_status != "pending":
            await db.execute(
                """
                UPDATE orders 
                SET status = $1, updated_at = NOW() 
                WHERE order_code = $2
                """,
                new_status,
                order_id
            )
        
        await db.execute(
            "INSERT INTO processed_webhooks (payment_id) VALUES ($1)",
            payment_id
        )
        
        logger.info(f"✅ Webhook обработан: PaymentId={payment_id}, Status={new_status}")
        
        return {"Status": "OK"}
        
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА в webhook: {e}", exc_info=True)
        return {"Status": "OK"}

@app.get("/orders")
async def get_user_orders(
    current_user: dict = Depends(get_current_user),
    _: bool = Depends(require_db_connection)
):
    """
    Возвращает список заказов конкретного пользователя.
    """
    try:
        tg_id_int = current_user["tg_id"]
        
        rows = await db.fetch(
            """
            SELECT order_code, total_rub, status, payment_method, created_at
            FROM orders
            WHERE tg_id = $1
            ORDER BY created_at DESC
            """,
            tg_id_int
        )
        
        orders = [
            {
                "order_code": row["order_code"],
                "total_rub": float(row["total_rub"]),
                "status": row["status"],
                "payment_method": row["payment_method"],
                "created_at": row["created_at"].isoformat()
            }
            for row in rows
        ]
        
        return {"success": True, "orders": orders}

    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный tg_id")
    except Exception as e:
        logger.error(f"Ошибка получения заказов для tg_id {tg_id_int}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/orders/{order_code}")
async def get_order_details(
    order_code: str,
    current_user: dict = Depends(get_current_user),
    _: bool = Depends(require_db_connection)
):
    """
    Возвращает полную информацию о конкретном заказе.
    """
    try:
        tg_id_int = current_user["tg_id"]
        
        order_row = await db.fetchrow(
            """
            SELECT order_code, total_rub, status, payment_method, created_at, 
                   client_email, client_phone, client_comment
            FROM orders
            WHERE order_code = $1 AND tg_id = $2
            """,
            order_code, tg_id_int
        )
        
        if not order_row:
            raise HTTPException(status_code=404, detail="Заказ не найден или доступ запрещен")
        
        items_rows = await db.fetch(
            """
            SELECT product_name, config, price_rub, delivery_days
            FROM order_items
            WHERE order_code = $1
            """,
            order_code
        )
        
        items = []
        for row in items_rows:
            items.append({
                "product_name": row["product_name"],
                "config": row["config"],
                "price_rub": float(row["price_rub"]),
                "delivery_days": row["delivery_days"]
            })
        
        return {
            "success": True,
            "order": {
                "order_code": order_row["order_code"],
                "total_rub": float(order_row["total_rub"]),
                "status": order_row["status"],
                "payment_method": order_row["payment_method"],
                "created_at": order_row["created_at"].isoformat(),
                "client_email": order_row["client_email"],
                "client_phone": order_row["client_phone"],
                "client_comment": order_row["client_comment"],
                "items": items
            }
        }

    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный tg_id")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка получения деталей заказа {order_code}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/orders/{order_code}/cancel")
async def cancel_order(
    order_code: str,
    current_user: dict = Depends(get_current_user),
    _: bool = Depends(require_db_connection)
):
    """
    Отменяет заказ, если он еще в статусе 'pending', и отправляет уведомления.
    """
    try:
        tg_id_int = current_user["tg_id"]
        
        order_row = await db.fetchrow(
            """
            SELECT status, total_rub, tg_id, telegram_first_name
            FROM orders
            WHERE order_code = $1 AND tg_id = $2
            """,
            order_code, tg_id_int
        )
        
        if not order_row:
            raise HTTPException(status_code=404, detail="Заказ не найден или доступ запрещен")
        
        if order_row["status"] != "pending":
            raise HTTPException(status_code=400, detail="Нельзя отменить заказ с текущим статусом")
        
        await db.execute(
            """
            UPDATE orders
            SET status = 'cancelled', updated_at = NOW()
            WHERE order_code = $1
            """,
            order_code
        )
        
        try:
            await send_telegram_notifications(
                order_id=order_code,
                amount_rub=float(order_row["total_rub"]),
                event="cancelled",
                telegram_id=str(order_row["tg_id"]) if order_row["tg_id"] else None,
                telegram_first_name=order_row["telegram_first_name"]
            )
        except Exception as e:
            logger.error(f"⚠️ Ошибка отправки уведомления об отмене: {e}")

        logger.info(f"✅ Заказ {order_code} успешно отменен пользователем {tg_id_int}")
        
        return {"success": True, "message": "Order cancelled successfully"}
        
    except ValueError:
        raise HTTPException(status_code=400, detail="Некорректный tg_id")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Ошибка отмены заказа {order_code}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

async def send_invoice_to_telegram(
    invoice_number: str,
    pdf_bytes: bytes,
    telegram_id: str | None,
    total_rub: float
):
    """Отправляет PDF-счёт в Telegram как файл"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    
    if not bot_token:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN не задан")
        return
    
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    
    caption = f"""
📄 <b>СЧЁТ № {invoice_number}</b>
💰 <b>Сумма:</b> {total_rub:,.2f} ₽

Оплатите по реквизитам в счёте.
После оплаты мы начнём работу над заказом.
""".strip()
    
    if telegram_id:
        try:
            files = {
                "document": (f"invoice_{invoice_number}.pdf", pdf_bytes, "application/pdf")
            }
            data = {
                "chat_id": str(telegram_id),
                "caption": caption,
                "parse_mode": "HTML"
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(url, data=data, files=files)
                if res.status_code == 200:
                    logger.info(f"✅ Счёт отправлен пользователю (TG ID: {telegram_id})")
                else:
                    logger.warning(f"⚠️ Не удалось отправить пользователю: {res.text}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки пользователю: {e}")
    
    if admin_chat_id:
        try:
            files = {
                "document": (f"invoice_{invoice_number}.pdf", pdf_bytes, "application/pdf")
            }
            data = {
                "chat_id": str(admin_chat_id),
                "caption": f"📄 <b>НОВЫЙ СЧЁТ</b> № {invoice_number}\n💰 {total_rub:,.2f} ₽",
                "parse_mode": "HTML"
            }
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.post(url, data=data, files=files)
                if res.status_code == 200:
                    logger.info(f"✅ Счёт отправлен админу")
                else:
                    logger.error(f"❌ Не удалось отправить админу: {res.text}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки админу: {e}")

async def send_telegram_notifications(
    order_id: str,
    amount_rub: float,
    event: str,
    payment_url: str | None = None,
    telegram_id: str | None = None,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    email: str = "",
    phone: str = "",
    method: str = "",
    comment: str = "",
    is_price_suspicious: bool = False,
    client_submitted_total: float | None = None
):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not bot_token:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN не задан")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        
        if telegram_id:
            user_name = telegram_first_name or "Клиент"
            
            if event == "created" and payment_url:
                user_msg = f"""
👋 Здравствуйте, {user_name}!
Мы получили ваш заказ <b>#{order_id}</b>.
💰 <b>Сумма к оплате:</b> {amount_rub:,.2f} ₽

Вы можете оплатить заказ, перейдя по ссылке ниже:
👉 <a href="{payment_url}">Оплатить заказ</a>

Если у вас есть вопросы, наша поддержка всегда на связи!
""".strip()
            elif event == "paid":
                user_msg = f"""
✅ <b>Заказ оплачен!</b>
Здравствуйте, {user_name}!
Ваш заказ <b>#{order_id}</b> успешно оплачен.
Мы уже начали его обработку. Спасибо за покупку!
""".strip()
            elif event == "cancelled":
                user_msg = f"""
⚠️ <b>Заказ отменен</b>
Здравствуйте, {user_name}!
Ваш заказ <b>#{order_id}</b> на сумму {amount_rub:,.2f} ₽ был успешно отменен.

Если это была ошибка или у вас есть вопросы, пожалуйста, свяжитесь с нашей поддержкой.
""".strip()
            else:
                user_msg = None

            if user_msg:
                try:
                    res = await client.post(url, json={
                        "chat_id": str(telegram_id),
                        "text": user_msg,
                        "parse_mode": "HTML"
                    })
                    if res.status_code == 200:
                        logger.info(f"✅ Уведомление пользователю ({event}) отправлено")
                    else:
                        logger.warning(f"⚠️ Не удалось отправить пользователю: {res.text}")
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки пользователю: {e}")

        if admin_chat_id:
            if is_price_suspicious:
                admin_title = "🚨 <b>ПОДОЗРИТЕЛЬНЫЙ ЗАКАЗ</b> 🚨"
                price_warning = f"\n⚠️ <b>Сумма от клиента:</b> {client_submitted_total:,.2f} ₽ (ПОДМЕНА?)"
            elif event == "created":
                admin_title = "🛒 <b>НОВЫЙ ЗАКАЗ</b> (ожидает оплаты)"
            elif event == "paid":
                admin_title = "💳 <b>ЗАКАЗ ОПЛАЧЕН</b>"
            elif event == "cancelled":
                admin_title = "⚠️ <b>ЗАКАЗ ОТМЕНЕН КЛИЕНТОМ</b>"
            else:
                admin_title = "📦 <b>СТАТУС ЗАКАЗА ИЗМЕНЕН</b>"

            admin_msg = f"""
{admin_title}
🔢 <b>Заказ:</b> {order_id}
💰 <b>Сумма:</b> {amount_rub:,.2f} ₽
💳 <b>Оплата:</b> {method or 'не указана'}

👤 <b>Данные клиента:</b>
• ID Telegram: <code>{telegram_id or 'не указан'}</code>
• Username: @{telegram_username or 'не указан'}
• Имя: {telegram_first_name or 'не указано'}
• Email: {email or 'не указан'}
• Телефон: {phone or 'не указан'}
📝 <b>Комментарий:</b> {comment or 'нет'}

<a href="https://shop.bytewizard.ru/ru/profile">👉 Проверить в админке</a>
""".strip()
            
            try:
                res = await client.post(url, json={
                    "chat_id": str(admin_chat_id),
                    "text": admin_msg,
                    "parse_mode": "HTML"
                })
                if res.status_code == 200:
                    logger.info(f"✅ Уведомление админу ({event}) успешно отправлено")
                else:
                    logger.error(f"❌ Telegram API error (admin): {res.text}")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки админу: {e}")

@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request, _db: bool = Depends(require_db_connection)):
    """
    Обработчик вебхуков от Telegram Bot API (successful_payment)
    """
    try:
        data = await request.json()
        
        if "message" in data and "successful_payment" in data["message"]:
            payment = data["message"]["successful_payment"]
            order_id = payment["invoice_payload"]
            
            await db.execute(
                """
                UPDATE orders 
                SET status = 'paid', updated_at = NOW()
                WHERE order_code = $1
                """,
                order_id
            )
            logger.info(f"✅ Заказ {order_id} успешно оплачен через Telegram Stars!")
            
            order_total = await db.fetchval("SELECT total_rub FROM orders WHERE order_code = $1", order_id)
            await send_telegram_notifications(
                order_id=order_id,
                amount_rub=order_total or 0,
                event="paid",
                telegram_id=str(data["message"]["chat"]["id"])
            )

        return {"ok": True}
    except Exception as e:
        logger.error(f"❌ Ошибка Telegram webhook: {e}")
        return {"ok": True}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )