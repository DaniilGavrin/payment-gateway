import asyncio
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

        #  УВЕДОМЛЕНИЕ О СОЗДАНИИ ЗАКАЗА (сразу, не ждём оплаты)
        try:
            # create_task запускает функцию в фоне, не блокируя ответ API
            asyncio.create_task(
                send_telegram_notification(
                    order_id=payload.order_id,
                    amount_rub=payload.total_rub,
                    event="created",  # ← новый параметр
                    email=payload.contact_email,
                    phone=payload.contact_phone,
                    method=payload.payment_method
                )
            )
        except Exception as e:
            logger.error(f"⚠️ Не удалось запустить уведомление: {e}")
        
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


# 🔹 Вспомогательная функция для уведомлений
async def send_telegram_notification(
    order_id: str, 
    amount_rub: float, 
    event: str = "created",
    email: str = "", 
    phone: str = "", 
    method: str = ""
):
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID")
    
    if not bot_token or not chat_id:
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN или CHAT_ID не заданы")
        return

    # Разные заголовки для разных событий
    if event == "created":
        title = " <b>НОВЫЙ ЗАКАЗ</b> (ожидает оплаты)"
    else:
        title = "💳 <b>ЗАКАЗ ОПЛАЧЕН</b>"

    message = f"""
{title}

🔢 <b>Заказ:</b> {order_id}
💰 <b>Сумма:</b> {amount_rub:,.2f} ₽
📧 <b>Email:</b> {email or 'не указан'}
📱 <b>Телефон:</b> {phone or 'не указан'}
💳 <b>Оплата:</b> {method}

<a href="https://shop.bytewizard.ru/ru/profile">👉 Проверить в админке</a>
""".strip()

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if response.status_code == 200:
                logger.info(f"✅ Уведомление ({event}) отправлено в Telegram")
            else:
                logger.error(f"❌ Telegram API error: {response.text}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки в Telegram: {e}")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )