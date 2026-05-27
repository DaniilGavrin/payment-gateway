import asyncpg
import os

DATABASE_URL = os.getenv("DATABASE_URL")
print(f'DATABASE_URL: {DATABASE_URL}')

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=1,
            max_size=10,
            ssl="require"
        )

    async def disconnect(self):
        await self.pool.close()

    async def fetch(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetch(query, *args)
        
    async def fetchrow(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)


db = Database()