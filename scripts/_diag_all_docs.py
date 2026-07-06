"""Check SECOP for addendums/modifications linked to the contracts."""
import asyncio
import httpx

async def main():
    base = 'https://www.datos.gov.co/resource'
    DS_CONTRATOS = 'jbjy-vk9h'
    DS_DOCS = 'dmgg-8hin'
    
    # The contract 4161.010.26.1.155.2026 has estado=Modificado
    # Check what types of contracts exist for this cedula
    async with httpx.AsyncClient(timeout=30.0) as c:
        # All contract types for cedula
        r = await c.get(f'{base}/{DS_CONTRATOS}.json', params={
            'documento_proveedor': '1110547406',
            '$limit': '200'
        })
        rows = r.json() if r.status_code == 200 else []
        print(f'All SECOP contracts for cedula 1110547406: {len(rows)}')
        
        # Group by tipo_de_contrato
        tipos = {}
        for row in rows:
            tipo = row.get('tipo_de_contrato', 'NULL')
            tipos[tipo] = tipos.get(tipo, 0) + 1
        print('\nBy tipo_de_contrato:')
        for tipo, cnt in sorted(tipos.items(), key=lambda x: -x[1]):
            print(f'  {tipo}: {cnt}')
        
        # Get all docs for all processes of this cedula
        print('\nDocs per proceso:')
        all_procs = set(r.get('proceso_de_compra','') for r in rows if r.get('proceso_de_compra'))
        print(f'Total unique processes: {len(all_procs)}')
        
        total_unique_docs = {}
        for proc in sorted(all_procs):
            r2 = await c.get(f'{base}/{DS_DOCS}.json', params={
                'proceso': proc, '$limit': '500'
            })
            docs = r2.json() if r2.status_code == 200 else []
            if docs:
                print(f'  {proc}: {len(docs)} docs')
                for doc in docs:
                    total_unique_docs[doc.get('id_documento','')] = doc
        
        print(f'\nTotal unique docs across ALL contracts for cedula: {len(total_unique_docs)}')

asyncio.run(main())
