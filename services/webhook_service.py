import logging
logger = logging.getLogger(__name__)


async def is_webhook_processed(invoice_id: str) -> bool:
    """
    Проверяет, не обрабатывали ли уже этот invoice_id.
    Реализуй под свою БД/кеш (Redis, PostgreSQL, etc.)
    """
    # Пример для псевдокода:
    # return await redis.sismember("processed_webhooks", invoice_id)
    return False  # ← заглушка, замени на реальную проверку


async def mark_webhook_processed(invoice_id: str):
    """Помечает invoice_id как обработанный (для идемпотентности)."""
    # await redis.sadd("processed_webhooks", invoice_id)
    # await redis.expire("processed_webhooks", 86400)  # храним сутки
    pass  # ← заглушка


async def process_payment_webhook(invoice_id: str, order_id: str | None, status: str, invoice_info: dict | None):
    """
    Бизнес-логика: обновляем БД, шлём уведомления, активируем доступ.
    Выполняется после успешной проверки токена.
    """
    logger.info(f"🔄 Processing webhook: invoice_id={invoice_id}, order_id={order_id}, status={status}")

    # 🔹 Извлекаем данные из invoice_info (если есть)
    uuid = (invoice_info or {}).get("uuid")
    amount = (invoice_info or {}).get("amount")
    currency_code = (invoice_info or {}).get("currency", {}).get("code")

    # 🔹 Пример: обновление статуса в БД (псевдокод)
    # await db.execute(
    #     """
    #     UPDATE orders
    #     SET status = $1, paid_at = NOW(), crypto_tx_hash = $2
    #     WHERE order_id = $3 OR uuid = $4
    #     """,
    #     status,
    #     (invoice_info or {}).get("tx_hash"),
    #     order_id,
    #     uuid
    # )

    # 🔹 Если оплата прошла — можно отправить уведомление во Flutter через Firebase/APN
    # if status == "success":
    #     await send_push_notification(order_id, "Оплата подтверждена ✅")

    logger.info(f"✅ Processed: {order_id or uuid}")
