### Диаграмма запросов

``` mermaid

sequenceDiagram
    participant U as 👤 Пользователь (Фронт/Куки)
    participant PG as 💳 Твой Payment Gateway (FastAPI)
    participant DB as 🗄️ PostgreSQL (Прямой доступ PG)
    participant EXT as 🌍 Внешний Шлюз (Тинькофф/CryptoCloud)
    participant TG as 🤖 Telegram Bot

    Note over U,EXT: 🔹 1. Проверка авторизации
    U->>PG: POST /auth/verify {init_data}
    PG->>DB: SELECT 1 FROM users WHERE tg_id=$1
    alt Не найден
        PG-->>U: 403 "Запустите бота"
    else Найден
        PG-->>U: 200 OK
    end

    Note over U,EXT: 🔹 2. Создание заказа (PG → БД напрямую)
    U->>PG: POST /orders {cart, tg_id}
    PG->>DB: INSERT orders (status='pending', user_id=$1, total=$2)
    DB-->>PG: order_id
    PG-->>U: {order_id, amount, methods}

    Note over U,EXT: 🔹 3. Инициализация платежа
    U->>PG: POST /payment/init {order_id, method}
    PG->>DB: SELECT status FROM orders WHERE id=$1
    PG->>EXT: POST /v2/Init (или /invoice/create)
    EXT-->>PG: {payment_url, external_id}
    PG->>DB: UPDATE orders SET pg_id=$1, status='waiting_payment'
    PG-->>U: {redirect_url}

    Note over U,EXT: 🔹 4. Вебхук → PG обновляет БД напрямую
    EXT->>PG: POST /webhook {order_id, status, signature}
    PG->>PG: Верификация подписи + идемпотентность
    PG->>DB: UPDATE orders SET status=$1, paid_at=NOW()
    PG->>DB: INSERT payments_log (invoice_id, raw_payload, processed_at)
    PG-->>EXT: 200 OK
    PG->>TG: Отправка уведомления (async)

    Note over U,EXT: 🔹 5. Проверка статуса
    U->>PG: GET /status/{order_id}
    PG->>DB: SELECT status FROM orders WHERE id=$1
    PG-->>U: {status, message}
```