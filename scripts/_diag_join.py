"""Check secop_contratos duplicates and join with contratos."""
import asyncio
import os
os.environ['DATABASE_URL'] = 'postgresql+asyncpg://cashin:cashin_local@localhost:5432/cashin'

async def main():
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy import text
    engine = create_async_engine(os.environ['DATABASE_URL'], echo=False)
    async with AsyncSession(engine) as db:
        # Duplicates
        r = await db.execute(text('''
            SELECT numero_contrato, COUNT(*) as cnt
            FROM secop_contratos
            GROUP BY numero_contrato
            HAVING COUNT(*) > 1
        '''))
        dups = r.fetchall()
        print(f'Duplicates in secop_contratos: {len(dups)}')
        for d in dups:
            print(f'  {d[0]}: {d[1]} rows')

        # Join
        r2 = await db.execute(text('''
            SELECT c.numero_contrato, sc.numero_contrato, sc.referencia_del_contrato, sc.proceso_de_compra
            FROM contratos c
            LEFT JOIN secop_contratos sc ON sc.numero_contrato = c.numero_contrato
            ORDER BY c.numero_contrato
        '''))
        rows = r2.fetchall()
        print(f'\nContratos vs secop_contratos join ({len(rows)} rows):')
        for row in rows:
            sc_num = str(row[1])[:35] if row[1] else 'NULL'
            proc = row[3] or 'NULL'
            print(f'  [{str(row[0])[:38]:38}] sc={sc_num:35} proc={proc}')

asyncio.run(main())
