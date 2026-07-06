import asyncio, sys
sys.path.insert(0, r'c:/Users/User/Documents/workspace/cashing/cashing-backend')
from app.core.config import settings
from app.core.security import hash_password as get_password_hash
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

async def main():
    hashed = get_password_hash("pass")
    engine = create_async_engine(settings.DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE usuarios SET password_hash = :h WHERE email = :e"),
            {"h": hashed, "e": "juanp.gzmz@gmail.com"},
        )
    print("Password updated OK")

asyncio.run(main())
