import asyncio

from fastapi import HTTPException, status
from database.db import db


async def require_db_connection():
    """
    Проверяет доступность PostgreSQL.
    """

    try:
        is_connected = await asyncio.wait_for(
            db.check_connection(),
            timeout=2.0
        )

        if not is_connected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "success": False,
                    "error": "Database unavailable"
                }
            )

    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "success": False,
                "error": "Database timeout"
            }
        )

    return True