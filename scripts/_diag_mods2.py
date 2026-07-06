"""Check if contract modifications have their own SECOP entries."""
import asyncio
import httpx

async def main():
    base = 'https://www.datos.gov.co/resource'
    DS_CONTRATOS = 'jbjy-vk9h'
    DS_DOCS = 'dmgg-8hin'
    
    # The contract CO1.PCCNTR.8874900 (ref=4161.010.26.1.155.2026) estado=Modificado
    # Check if there's a modification entry in SECOP that references it
    async with httpx.AsyncClient(timeout=30.0) as c:
        # Look for entries where referencia contains the PCCNTR ID of the original
        r = await c.get(f'{base}/{DS_CONTRATOS}.json', params={
            '$where': "referencia_del_contrato LIKE 'CO1.PCCNTR.8874900%'",
            '$limit': '20'
        })
        rows = r.json() if r.status_code == 200 else []
        print(f'Contracts referencing CO1.PCCNTR.8874900: {len(rows)}')
        for row in rows:
            print(f"  id={row.get('id_contrato')} | ref={row.get('referencia_del_contrato')} | proc={row.get('proceso_de_compra')} | estado={row.get('estado_contrato')}")
        
        # Try looking for modifications by ID pattern
        r2 = await c.get(f'{base}/{DS_CONTRATOS}.json', params={
            '$where': "id_contrato = 'CO1.PCCNTR.8874900'",
            '$limit': '10'
        })
        rows2 = r2.json() if r2.status_code == 200 else []
        print(f'\nExact id=CO1.PCCNTR.8874900: {len(rows2)} rows')
        for row in rows2:
            print(f"  id={row.get('id_contrato')} | ref={row.get('referencia_del_contrato')} | proc={row.get('proceso_de_compra')} | estado={row.get('estado_contrato')} | fecha_fin={row.get('fecha_de_fin_del_contrato','')[:10] if row.get('fecha_de_fin_del_contrato') else '?'}")
        
        # Check if there are 2024/2025 contracts for cedula with 4161 prefix
        r3 = await c.get(f'{base}/{DS_CONTRATOS}.json', params={
            '$where': "documento_proveedor = '1110547406' AND referencia_del_contrato LIKE '4161%'",
            '$limit': '50'
        })
        rows3 = r3.json() if r3.status_code == 200 else []
        print(f'\nAll 4161.* contracts for cedula: {len(rows3)}')
        all_procs = set()
        for row in rows3:
            print(f"  {row.get('referencia_del_contrato','?')[:40]:40} | {row.get('proceso_de_compra','?'):25} | {row.get('estado_contrato','?')}")
            if row.get('proceso_de_compra'):
                all_procs.add(row.get('proceso_de_compra'))
        
        # Count docs for each
        print(f'\nDocs per proceso:')
        for proc in sorted(all_procs):
            r4 = await c.get(f'{base}/{DS_DOCS}.json', params={
                'proceso': proc, '$limit': '200'
            })
            docs = r4.json() if r4.status_code == 200 else []
            print(f'  {proc}: {len(docs)} docs')

asyncio.run(main())
