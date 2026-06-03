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
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse


from services.email_service import EmailService
from services.invoice_pdf_service import InvoicePDFService

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
        return False  # 🔒 В продакшене — строго False!

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

@app.get("/")
async def root():
    return {
        "status": "ok",
        "message": "FastAPI Payment Gateway is running"
    }

@app.post("/orders/create", response_model=OrderCreateOut)
async def create_order(
    payload: OrderCreateIn,
    _db: bool = Depends(require_db_connection)
):
    logger.info(f"📦 Новый заказ: {payload.order_id} | {payload.payment_method} | {payload.total_rub}₽")
    
    try:
        # 🔹 1. Сохраняем заказ в БД (таблица orders)
        tg_id_val = int(payload.telegram_id) if payload.telegram_id else None

        await db.execute(
            """
            INSERT INTO orders (
                order_code, client_email, client_phone, payment_method, 
                total_rub, status, 
                tg_id, telegram_username, telegram_first_name, telegram_last_name,
                client_comment, 
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW())
            """,
            payload.order_id,
            payload.contact_email,
            payload.contact_phone,
            payload.payment_method,
            payload.total_rub,
            'pending',
            tg_id_val,                      
            payload.telegram_username,      
            payload.telegram_first_name,    
            payload.telegram_last_name,
            payload.client_comment          # <-- Добавили комментарий сюда
        )
        
        # 🔹 2. Сохраняем позиции заказа (таблица order_items)
        for item in payload.items:
            await db.execute(
                """
                INSERT INTO order_items (
                    order_id, order_code, product_id, product_name,
                    config, price_rub, delivery_days, created_at
                ) VALUES (
                    -- Находим UUID заказа по order_code для связи
                    (SELECT id FROM orders WHERE order_code = $1),
                    $1, $2, $3, $4, $5, $6, NOW()
                )
                """,
                payload.order_id,
                item.product_id,
                item.product_name,
                json.dumps(item.config.dict()),  # 🔥 Конфиг как JSONB
                item.price_rub,
                item.delivery_days
            )
        
        # 🔹 3. Маршрутизация на платежный шлюз
        payment_url = None
        
        if payload.payment_method in ['card', 'sbp']:
            # 🏦 T-Bank (Tinkoff)
            payment_url = await _create_tbank_payment(payload)
            
        elif payload.payment_method == 'crypto':
            # ₿ CryptoCloud
            payment_url = await _create_cryptocloud_payment(payload)

        elif payload.payment_method == 'stars':
            payment_url = await _create_telegram_stars_payment(payload)
            
        elif payload.payment_method == 'invoice':
            # 📄 Генерация и отправка счёта
            invoice_url = await _generate_and_send_invoice(payload, _db)
            payment_url = invoice_url  # Возвращаем URL на PDF
        
        if not payment_url:
            raise HTTPException(status_code=502, detail="Не удалось создать ссылку на оплату")
        
        logger.info(f"✅ Заказ {payload.order_id} создан. Оплата: {payment_url}")

        # 🔹 ДЕБАГ: Явно логируем перед вызовом
        logger.info("📤 Пытаемся отправить уведомление в Telegram...")
        logger.info(f"   Order ID: {payload.order_id}")
        logger.info(f"   Total: {payload.total_rub}")
        logger.info(f"   Email: {payload.contact_email}")
        
        try:
            logger.info("📤 Начинаем отправку уведомлений в Telegram...")
            await send_telegram_notifications(
                order_id=payload.order_id,
                amount_rub=payload.total_rub,
                event="created",
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
    amount_kopecks = int(order.total_rub * 100)  # Т-Банк принимает только копейки (int)

    # 1. Параметры для генерации токена (Receipt сюда НЕ добавляем!)
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

    # 2. Формируем чек для 54-ФЗ (обязательно для Т-Банка)
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

    # 3. Собираем финальный пейлоад (Receipt добавляем ПОСЛЕ токена)
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
    
    # CryptoCloud принимает сумму в основной валюте (не копейки)
    data = {
        "amount": order.total_rub,
        "shop_id": os.getenv("CRYPTOCLOUD_SHOP_ID"),
        "currency": "RUB",  # или динамически из payload, если добавишь
        "order_id": order.order_id,
        "success_url": f"https://shop.bytewizard.ru/{order.locale}/profile?order={order.order_id}&status=success",
        "fail_url": f"https://shop.bytewizard.ru/{order.locale}/profile?order={order.order_id}&status=failed",
    }
    
    response = requests.post(url, headers=headers, json=data)
    
    if response.status_code == 200:
        result = response.json()["result"]
        return result["link"]  # Ссылка на оплату от CryptoCloud
    else:
        logger.error(f"CryptoCloud error: {response.text}")
        raise Exception(f"CryptoCloud API error: {response.text[:200]}")
    

async def _create_telegram_stars_payment(order: OrderCreateIn) -> str:
    """Создает инвойс в Telegram Stars и возвращает прямую ссылку на оплату"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise Exception("TELEGRAM_BOT_TOKEN не задан в .env")
    
    # Конвертация рублей в звезды (1 звезда ≈ 1.5-2₽, настрой под свой курс)
    # Для валюты XTR сумма указывается в ЦЕЛЫХ звездах (без копеек!)
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

async def _generate_and_send_invoice(order: OrderCreateIn, db) -> str:
    """
    Генерирует PDF, сохраняет данные в БД, отправляет в Telegram и на email.
    Возвращает URL для скачивания PDF.
    """
    try:
        # 1. Генерируем номер счёта
        invoice_number = await db.fetchval(
            "SELECT COALESCE(MAX(invoice_number), 'INV-0000') FROM invoices"
        )
        # Увеличиваем номер (INV-0001 -> INV-0002)
        prefix = "INV-"
        num = int(invoice_number.split("-")[1]) + 1
        invoice_number = f"{prefix}{num:04d}"
        
        # 2. Данные покупателя
        buyer_data = {
            "first_name": order.telegram_first_name,
            "last_name": order.telegram_last_name,
            "email": order.contact_email,
            "phone": order.contact_phone,
            "company_name": None,  # Позже добавим в форму
            "inn": None,
        }
        
        # 3. Генерируем PDF (для отправки)
        pdf_bytes = invoice_pdf_service.generate_invoice_pdf(
            invoice_number=invoice_number,
            order={
                "items": [item.dict() for item in order.items],
                "total_rub": order.total_rub,
            },
            seller=COMPANY_REQUISITES,
            buyer=buyer_data
        )
        
        # 4. Сохраняем ДАННЫЕ в БД (не PDF!)
        await db.execute(
            """
            INSERT INTO invoices (
                invoice_number, order_code, buyer_data, seller_data, 
                items_data, total_rub, status
            ) VALUES ($1, $2, $3, $4, $5, $6, 'generated')
            """,
            invoice_number,
            order.order_id,
            json.dumps(buyer_data),
            json.dumps(COMPANY_REQUISITES),
            json.dumps([item.dict() for item in order.items]),
            order.total_rub
        )
        
        # 5. Отправляем PDF в Telegram (как файл)
        await send_invoice_to_telegram(
            invoice_number=invoice_number,
            pdf_bytes=pdf_bytes,
            telegram_id=order.telegram_id,
            total_rub=order.total_rub
        )
        
        # 6. Отправляем PDF на email (как вложение)
        await email_service.send_invoice_email(
            to_email=order.contact_email,
            invoice_number=invoice_number,
            pdf_bytes=pdf_bytes,
            total_rub=order.total_rub
        )
        
        # 7. Возвращаем URL для скачивания
        return f"https://pay.bytewizard.ru/invoice/{invoice_number}/download"
        
    except Exception as e:
        logger.error(f"❌ Ошибка создания счёта: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Не удалось создать счёт")

@app.get("/orders/check")
async def check_orders():
    # проверяем с фронта прошел ли платеж, и возвращаем статусы "отменем, или же успех" или как-то ещё чтобы фронт понимал если платеж не прошел то просто ждем так как может быть несколько попыток оплаты мало ли что косячит клиент
    pass

@app.post("/api/crypto-cloud/list-payments")
async def cryptocloud_list_payments(req: PaymentList, _: str = Depends(verify_admin_token), _db: bool = Depends(require_db_connection)):
    url = f"{os.getenv("CRYPTOCLOUD_API_URL")}invoice/merchant/list"

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
async def cryptocloud_webhook(request: Request, _db: bool = Depends(require_db_connection) ):
    """
    Обработчик POSTBACK от CryptoCloud (только JSON).
    Отвечает быстро, тяжёлую логику выносит в process_payment_webhook.
    """

    # 🔹 1. Быстрая проверка Content-Type
    if not request.headers.get("Content-Type", "").startswith("application/json"):
        logger.warning(f"❌ Wrong Content-Type: {request.headers.get('Content-Type')}")
        raise HTTPException(status_code=415, detail="Only application/json accepted")

    # 🔹 2. Парсим JSON (FastAPI сделает это эффективно)
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"❌ JSON parse error: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 🔹 3. Валидация через Pydantic (опционально, но даёт чёткие ошибки)
    try:
        webhook = CryptoCloudWebhook(**body)
    except Exception as e:
        logger.warning(f"⚠️ Validation error: {e}")
        raise HTTPException(status_code=422, detail=f"Invalid payload: {str(e)}")

    # 🔹 4. Проверка подписи (самое важное!)
    if not verify_cryptocloud_jwt(webhook.token):
        raise HTTPException(status_code=401, detail="Invalid token signature")

    # 🔹 5. Идемпотентность: не обрабатывай дубли
    if await is_webhook_processed(webhook.invoice_id):
        logger.info(f"⏭️ Duplicate webhook, skipping: {webhook.invoice_id}")
        return JSONResponse(status_code=200, content={"status": "ok", "skipped": True})

    # 🔹 6. Асинхронно обрабатываем бизнес-логику (не блокируя ответ)
    # Если логика тяжёлая — вынеси в очередь задач (Celery/RQ)
    await process_payment_webhook(
        invoice_id=webhook.invoice_id,
        order_id=webhook.order_id,
        status=webhook.status,
        invoice_info=webhook.invoice_info
    )

    # 🔹 7. Помечаем как обработанный
    await mark_webhook_processed(webhook.invoice_id)

    # 🔹 8. Отвечаем быстро — CryptoCloud ждёт 200
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
        
        # 🔹 1. Логируем всё (для отладки)
        logger.info(f"🔔 Webhook от Т-Банка: {json.dumps(data, ensure_ascii=False)}")
        
        payment_id = data.get("PaymentId")
        order_id = data.get("OrderId")
        status = data.get("Status")
        amount = data.get("Amount")
        
        # 🔹 2. Проверка токена (БЕЗОПАСНОСТЬ!)
        if not generator.verify_tbank_webhook_token(data, os.getenv("SECRET_PASSWORD")):
            logger.error(f"❌ НЕВЕРНЫЙ ТОКЕН от Т-Банка! PaymentId={payment_id}")
            return {"Status": "ERROR"}
        
        # 🔹 3. Идемпотентность (проверяем, не обрабатывали ли уже)
        exists = await db.fetchval(
            "SELECT 1 FROM processed_webhooks WHERE payment_id = $1",
            payment_id
        )
        if exists:
            logger.info(f"⏭️ Webhook уже обработан: PaymentId={payment_id}")
            return {"Status": "OK"}  # Т-Банк требует OK даже для дублей
        
        # 🔹 4. Обновляем статус заказа
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
            # Это промежуточный статус, не меняем основной статус
            logger.info(f"⏳ Заказ {order_id}: 3D-Secure проверен, ждём CONFIRMED")
            await db.execute(
                "UPDATE orders SET status = '3ds_checked', updated_at = NOW() WHERE order_code = $1",
                order_id
            )
            # Помечаем вебхук как обработанный
            await db.execute(
                "INSERT INTO processed_webhooks (payment_id) VALUES ($1)",
                payment_id
            )
            return {"Status": "OK"}
            
        else:
            logger.warning(f"⚠️ Неизвестный статус {status} для заказа {order_id}")
        
        # Обновляем статус в БД (если не 3DS_CHECKED)
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
        
        # 🔹 6. Помечаем вебхук как обработанный
        await db.execute(
            "INSERT INTO processed_webhooks (payment_id) VALUES ($1)",
            payment_id
        )
        
        logger.info(f"✅ Webhook обработан: PaymentId={payment_id}, Status={new_status}")
        
        # Т-Банк требует ответ {"Status": "OK"}
        return {"Status": "OK"}
        
    except Exception as e:
        logger.error(f"❌ КРИТИЧЕСКАЯ ОШИБКА в webhook: {e}", exc_info=True)
        # Даже при ошибке возвращаем OK, чтобы Т-Банк не спамил
        return {"Status": "OK"}

@app.get("/orders")
async def get_user_orders(
    tg_id: str = Query(..., description="Telegram ID пользователя"),
    _: bool = Depends(require_db_connection)
):
    """
    Возвращает список заказов конкретного пользователя.
    Используется для страницы профиля.
    """
    try:
        tg_id_int = int(tg_id)
        
        # Забираем заказы, отсортированные от новых к старым
        rows = await db.fetch(
            """
            SELECT order_code, total_rub, status, payment_method, created_at
            FROM orders
            WHERE tg_id = $1
            ORDER BY created_at DESC
            """,
            tg_id_int
        )
        
        # Форматируем в удобный для фронта JSON
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
        logger.error(f"Ошибка получения заказов для tg_id {tg_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/orders/{order_code}")
async def get_order_details(
    order_code: str,
    tg_id: str = Query(..., description="Telegram ID пользователя для проверки прав"),
    _: bool = Depends(require_db_connection)
):
    """
    Возвращает полную информацию о конкретном заказе (включая items и config).
    """
    try:
        tg_id_int = int(tg_id)
        
        # 1. Проверяем, что заказ существует и принадлежит этому пользователю
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
        
        # 2. Получаем все позиции этого заказа
        items_rows = await db.fetch(
            """
            SELECT product_name, config, price_rub, delivery_days
            FROM order_items
            WHERE order_code = $1
            """,
            order_code
        )
        
        # 3. Формируем красивый ответ
        items = []
        for row in items_rows:
            items.append({
                "product_name": row["product_name"],
                "config": row["config"],  # Это уже dict (JSONB) благодаря asyncpg
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
    payload: dict,
    _: bool = Depends(require_db_connection)
):
    """
    Отменяет заказ, если он еще в статусе 'pending', и отправляет уведомления.
    """
    try:
        tg_id_int = int(payload.get("tg_id", 0))
        
        # 1. Получаем данные заказа для проверки прав и для уведомлений
        # 🔹 ИСПРАВЛЕНО: telegram_id заменено на tg_id
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
        
        # 2. Проверяем статус (отменить можно только 'pending')
        if order_row["status"] != "pending":
            raise HTTPException(status_code=400, detail="Нельзя отменить заказ с текущим статусом")
        
        # 3. Обновляем статус на 'cancelled'
        await db.execute(
            """
            UPDATE orders
            SET status = 'cancelled', updated_at = NOW()
            WHERE order_code = $1
            """,
            order_code
        )
        
        # 4. Отправляем уведомления об отмене (пользователю и админу)
        try:
            await send_telegram_notifications(
                order_id=order_code,
                amount_rub=float(order_row["total_rub"]),
                event="cancelled",
                # 🔹 ИСПРАВЛЕНО: берем значение из правильной колонки tg_id
                telegram_id=str(order_row["tg_id"]) if order_row["tg_id"] else None,
                telegram_first_name=order_row["telegram_first_name"]
            )
        except Exception as e:
            # Не ломаем ответ фронта, если уведомление не ушло, просто логируем
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

@app.get("/invoice/{invoice_number}/download")
async def download_invoice(
    invoice_number: str,
    _db: bool = Depends(require_db_connection)
):
    """Генерирует PDF на лету и отдаёт клиенту"""
    
    # 1. Получаем данные из БД
    invoice_data = await db.fetchrow(
        """
        SELECT buyer_data, seller_data, items_data, total_rub
        FROM invoices
        WHERE invoice_number = $1
        """,
        invoice_number
    )
    
    if not invoice_data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # 2. Генерируем PDF
    pdf_bytes = invoice_pdf_service.generate_invoice_pdf(
        invoice_number=invoice_number,
        order={
            "items": invoice_data["items_data"],
            "total_rub": float(invoice_data["total_rub"])
        },
        seller=invoice_data["seller_data"],
        buyer=invoice_data["buyer_data"]
    )
    
    # 3. Отдаём как файл
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=invoice_{invoice_number}.pdf"
        }
    )


# ========================================================================
# 🔹 Эндпоинт для просмотра PDF в браузере (inline)
# ========================================================================
@app.get("/invoice/{invoice_number}/view")
async def view_invoice(
    invoice_number: str,
    _db: bool = Depends(require_db_connection)
):
    """Открывает PDF в браузере (встроенный просмотр)"""
    
    invoice_data = await db.fetchrow(
        """
        SELECT buyer_data, seller_data, items_data, total_rub
        FROM invoices
        WHERE invoice_number = $1
        """,
        invoice_number
    )
    
    if not invoice_data:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    pdf_bytes = invoice_pdf_service.generate_invoice_pdf(
        invoice_number=invoice_number,
        order={
            "items": invoice_data["items_data"],
            "total_rub": float(invoice_data["total_rub"])
        },
        seller=invoice_data["seller_data"],
        buyer=invoice_data["buyer_data"]
    )
    
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=invoice_{invoice_number}.pdf"
        }
    )

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
    
    # Текст сообщения
    caption = f"""
📄 <b>СЧЁТ № {invoice_number}</b>
💰 <b>Сумма:</b> {total_rub:,.2f} ₽

Оплатите по реквизитам в счёте.
После оплаты мы начнём работу над заказом.
""".strip()
    
    # Отправка пользователю
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
    
    # Отправка админу
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
    event: str,  # "created", "paid" или "cancelled"
    payment_url: str | None = None,
    telegram_id: str | None = None,
    telegram_username: str | None = None,
    telegram_first_name: str | None = None,
    email: str = "",
    phone: str = "",
    method: str = "",
    comment: str = ""
):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    if not bot_token:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN не задан")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10.0) as client:
        
        # ========================================================================
        # 1. УВЕДОМЛЕНИЕ ПОЛЬЗОВАТЕЛЮ
        # ========================================================================
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

        # ========================================================================
        # 2. УВЕДОМЛЕНИЕ АДМИНУ (Всегда)
        # ========================================================================
        if admin_chat_id:
            if event == "created":
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
        
        # Проверяем, что это успешная оплата
        if "message" in data and "successful_payment" in data["message"]:
            payment = data["message"]["successful_payment"]
            order_id = payment["invoice_payload"]
            
            # Обновляем статус заказа в БД
            await db.execute(
                """
                UPDATE orders 
                SET status = 'paid', updated_at = NOW()
                WHERE order_code = $1
                """,
                order_id
            )
            logger.info(f"✅ Заказ {order_id} успешно оплачен через Telegram Stars!")
            
            # Отправляем уведомление админу
            # Сумму берем из БД, чтобы не передавать лишнее
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