import logging
from database.db import db

logger = logging.getLogger(__name__)


async def is_webhook_processed(invoice_id: str) -> bool:
    """
    Проверяет, не обрабатывали ли уже этот invoice_id.
    Используем таблицу processed_webhooks (как для Т-Банка).
    """
    try:
        exists = await db.fetchval(
            "SELECT 1 FROM processed_webhooks WHERE payment_id = $1",
            invoice_id
        )
        return exists is not None
    except Exception as e:
        logger.error(f"❌ Ошибка проверки processed_webhooks: {e}")
        return False


async def mark_webhook_processed(invoice_id: str):
    """Помечает invoice_id как обработанный (для идемпотентности)."""
    try:
        await db.execute(
            "INSERT INTO processed_webhooks (payment_id) VALUES ($1)",
            invoice_id
        )
        logger.info(f"✅ Webhook {invoice_id} помечен как обработанный")
    except Exception as e:
        logger.error(f"❌ Ошибка маркировки processed_webhooks: {e}")


async def process_payment_webhook(invoice_id: str, order_id: str | None, status: str, invoice_info: dict | None):
    """
    Бизнес-логика: обновляем БД, шлём уведомления.
    Выполняется после успешной проверки токена.
    """
    logger.info(f"🔄 Processing webhook: invoice_id={invoice_id}, order_id={order_id}, status={status}")

    if not order_id:
        logger.warning(f"⚠️ Нет order_id в webhook, пропускаем")
        return

    # 🔹 Маппинг статусов CryptoCloud на наши
    status_map = {
        "success": "paid",
        "paid": "paid",
        "fail": "failed",
        "failed": "failed",
        "cancel": "cancelled",
        "cancelled": "cancelled",
        "pending": "pending",
    }
    
    new_status = status_map.get(status.lower(), status)
    
    # 🔹 Обновляем статус заказа в БД
    try:
        await db.execute(
            """
            UPDATE orders 
            SET status = $1, updated_at = NOW()
            WHERE order_code = $2
            """,
            new_status,
            order_id
        )
        logger.info(f"✅ Статус заказа {order_id} обновлён на {new_status}")
    except Exception as e:
        logger.error(f"❌ Ошибка обновления заказа {order_id}: {e}")
        return

    # 🔹 Если оплата прошла — отправляем уведомления
    if new_status == "paid":
        try:
            # Получаем данные заказа для уведомлений
            order_data = await db.fetchrow(
                """
                SELECT tg_id, telegram_username, telegram_first_name, 
                       client_email, client_phone, payment_method, total_rub
                FROM orders WHERE order_code = $1
                """,
                order_id
            )
            
            if order_data:
                # Импортируем функцию отправки уведомлений
                from main import send_telegram_notifications
                
                await send_telegram_notifications(
                    order_id=order_id,
                    amount_rub=float(order_data["total_rub"]),
                    event="paid",
                    telegram_id=str(order_data["tg_id"]) if order_data["tg_id"] else None,
                    telegram_username=order_data["telegram_username"],
                    telegram_first_name=order_data["telegram_first_name"],
                    email=order_data["client_email"] or "",
                    phone=order_data["client_phone"] or "",
                    method=order_data["payment_method"] or "crypto"
                )
                logger.info(f"✅ Уведомления об оплате отправлены для заказа {order_id}")
        except Exception as e:
            logger.error(f"⚠️ Ошибка отправки уведомлений для заказа {order_id}: {e}", exc_info=True)

    logger.info(f"✅ Webhook обработан: invoice_id={invoice_id}, order_id={order_id}, status={new_status}")