import os
import requests
import jwt
import logging
import json
import time
import hashlib
import hmac
import http.client
import generator


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
from fastapi.responses import JSONResponse


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET")

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
    allow_origins=["*"],
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
        await db.execute(
            """
            INSERT INTO orders (
                order_code, client_email, client_phone, payment_method, 
                total_rub, status, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
            """,
            payload.order_id,
            payload.contact_email,
            payload.contact_phone,
            payload.payment_method,
            payload.total_rub,
            'pending'  # начальный статус
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
            
        elif payload.payment_method == 'invoice':
            # 📄 Для юр. лиц — пока заглушка (отправка счета на email)
            await _send_invoice_email(payload)
            payment_url = f"/profile?order={payload.order_id}&status=invoice_sent"
        
        if not payment_url:
            raise HTTPException(status_code=502, detail="Не удалось создать ссылку на оплату")
        
        logger.info(f"✅ Заказ {payload.order_id} создан. Оплата: {payment_url}")
        
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
    # Используем твою существующую логику, адаптированную под динамические данные
    payment_params = {
        "TerminalKey": os.getenv("TERMINAL_KEY"),
        "Amount": int(order.total_rub * 100),  # Tinkoff принимает копейки
        "OrderId": order.order_id,
        "Description": f"Заказ в ByteWizard: {order.items[0].product_name}",
        "CustomerKey": order.contact_email,  # или хэш от email
        "PayType": "O",
        "Language": order.locale,
    }
    
    # Генерация токена (твоя существующая функция)
    token = generator.generate_tinkoff_token(payment_params, os.getenv("SECRET_PASSWORD"))
    
    full_payload = {
        **payment_params,
        "Token": token,
        "DATA": {
            "Email": order.contact_email,
            "Phone": order.contact_phone or "",
            "OperationInitiatorType": "0"
        },
    }
    
    # Отправка запроса (синхронный requests в асинхронной функции — не идеально,
    # но для старта ок. В продакшене лучше httpx или aiohttp)
    conn = http.client.HTTPSConnection(os.getenv("TINKOFF_API_URL"))
    headers = {"Content-Type": "application/json"}
    
    try:
        conn.request(
            "POST", "/v2/Init",
            body=json.dumps(full_payload, ensure_ascii=False),
            headers=headers
        )
        response = conn.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        
        if result.get("Success"):
            return result["PaymentURL"]  # Ссылка на оплату от Tinkoff
        else:
            logger.error(f"T-Bank error: {result}")
            raise Exception(f"T-Bank API error: {result.get('Message')}")
            
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

async def _send_invoice_email(order: OrderCreateIn):
    """Заглушка: отправка счета на email для юр. лиц"""
    # Тут можно подключить SMTP и отправить письмо с реквизитами
    logger.info(f"📧 Отправка счета для {order.contact_email} на сумму {order.total_rub}₽")
    # Пример: await send_email(to=order.contact_email, subject=f"Счет #{order.order_id}", ...)
    pass

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

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )