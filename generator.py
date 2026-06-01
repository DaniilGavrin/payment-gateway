import hashlib

def generate_tinkoff_token(payload: dict, password: str) -> str:
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
    Т-Банк присылает Token, который нужно пересчитать и сравнить.
    """
    received_token = data.get("Token")
    if not received_token:
        return False
    
    # Убираем Token из данных для проверки
    data_copy = {k: v for k, v in data.items() if k != "Token"}
    
    # Сортируем ключи и конкатенируем значения
    sorted_values = ""
    for key in sorted(data_copy.keys()):
        value = data_copy[key]
        if value is not None:
            sorted_values += str(value)
    
    # Добавляем секретный пароль
    sorted_values += secret_password
    
    # Хешируем SHA256
    calculated_token = hashlib.sha256(sorted_values.encode('utf-8')).hexdigest().upper()
    
    return calculated_token == received_token.upper()