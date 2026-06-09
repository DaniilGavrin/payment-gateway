import hashlib

def generate_tinkoff_token(payload: dict, password: str) -> str:
    """Генерация токена для запроса к Т-Банку."""
    flat_params = {
        key: str(value) if value is not None else ""
        for key, value in payload.items()
        if not isinstance(value, (dict, list))
    }
    flat_params["Password"] = password
    sorted_params = sorted(flat_params.items(), key=lambda x: x[0])
    concatenated = "".join(value for _, value in sorted_params)
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()

def verify_tbank_webhook_token(data: dict, secret_password: str) -> bool:
    """
    Проверяет токен от Т-Банка.
    
    Алгоритм по документации:
    1. Берём все поля webhook КРОМЕ Token
    2. Добавляем Password = secret_password
    3. Сортируем ВСЕ ключи по алфавиту (включая Password!)
    4. Конкатенируем значения (None → "")
    5. SHA256 → сравниваем с Token
    """
    received_token = data.get("Token")
    if not received_token:
        return False
    
    # 1. Копируем данные БЕЗ Token
    data_copy = {k: v for k, v in data.items() if k != "Token"}
    
    # 2. Добавляем Password КАК ОБЫЧНЫЙ ПАРАМЕТР (участвует в сортировке!)
    data_copy["Password"] = secret_password
    
    # 3. Сортируем ключи и конкатенируем значения
    sorted_values = ""
    for key in sorted(data_copy.keys()):
        value = data_copy[key]
        
        if isinstance(value, bool):
            sorted_values += "true" if value else "false"
        elif value is None:
            sorted_values += ""
        else:
            sorted_values += str(value)
    
    # 4. Хешируем SHA256
    calculated_token = hashlib.sha256(sorted_values.encode('utf-8')).hexdigest()
    
    # 5. Сравниваем (в нижнем регистре, без upper())
    return calculated_token.lower() == received_token.lower()