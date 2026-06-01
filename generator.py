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