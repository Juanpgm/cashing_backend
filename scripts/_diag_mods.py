"""Quick diagnostic: check for contract modifications in SECOP."""
import asyncio
import httpx

async def main():
    base = 'https://www.datos.gov.co/resource'
    DS_CONTRATOS = 'jbjy-vk9h'
    DS_DOCS = 'dmgg-8hin'
    
    async with httpx.AsyncClient(timeout=30.0) as c:
        # Check if there are more contract entries with LIKE
        r = await c.get(f'{base}/{DS_CONTRATOS}.json', params={
            '$where': "id_contrato LIKE 'CO1.PCCNTR.8874%'",
            '$limit': '50'
        })
        rows = r.json() if r.status_code == 200 else []
        print(f'SECOP rows with id LIKE CO1.PCCNTR.8874%: {len(rows)}')
        for row in rows:
            print(f"  id={row.get('id_contrato')} | ref={row.get('referencia_del_contrato')} | proc={row.get('proceso_de_compra')} | estado={row.get('estado_contrato')}")
        
        # Check all docs for CO1.BDOS.9493653 (including all fields)
        r2 = await c.get(f'{base}/{DS_DOCS}.json', params={
            'proceso': 'CO1.BDOS.9493653',
            '$limit': '500'
        })
        docs = r2.json() if r2.status_code == 200 else []
        print(f'\nAll docs for proceso CO1.BDOS.9493653: {len(docs)}')
        for doc in docs:
            ext = doc.get('extensi_n', '?')
            nombre = str(doc.get('nombre_archivo', '?'))[:55]
            num_c = doc.get('n_mero_de_contrato', '')
            print(f"  [{ext:6}] {nombre} | num_contrato={num_c}")
        
        # Try proceso dataset for process info
        r3 = await c.get(f'{base}/p6dx-8zbt.json', params={
            'id_del_portafolio': 'CO1.BDOS.9493653',
            '$limit': '5'
        })
        p_rows = r3.json() if r3.status_code == 200 else []
        print(f'\nProceso CO1.BDOS.9493653 info: {len(p_rows)} rows')
        for row in p_rows:
            print(f"  fase={row.get('fase')} | nombre={str(row.get('nombre_del_procedimiento',''))[:60]}")

asyncio.run(main())
