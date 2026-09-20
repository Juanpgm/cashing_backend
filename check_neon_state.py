import asyncio, asyncpg, os

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    ver = await conn.fetchval("select version_num from alembic_version")
    print("alembic_version:", ver)
    t = await conn.fetchval("select exists (select 1 from pg_type where typname=$1)", "tipo_adicion")
    print("tipo_adicion enum exists:", t)
    tb = await conn.fetchval("select exists (select 1 from information_schema.tables where table_name=$1)", "adiciones_contrato")
    print("adiciones_contrato table exists:", tb)
    await conn.close()

asyncio.run(main())
