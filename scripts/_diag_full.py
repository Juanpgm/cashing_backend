"""
Diagnóstico completo: cuántos documentos/contratos hay en SECOP vs qué devuelve la API.
Uso:
    python scripts/_diag_full.py <cedula>
"""
from __future__ import annotations
import asyncio, sys, json
import httpx

BASE = "https://www.datos.gov.co/resource"
DS_CONTRATOS = "jbjy-vk9h"
DS_DOCS = [
    ("f8va-cf4m", "docs_hist ≤2021"),
    ("kgcd-kt7i", "docs_2022"),
    ("3skv-9na7", "docs_2023"),
    ("dmgg-8hin", "docs_2025"),
]
DS_MODS = "u8cx-r425"


async def main(cedula: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:

        # ── 1. Todos los contratos en Socrata (sin filtro de tipo)
        print(f"\n{'='*60}")
        print(f"CONTRATOS EN SECOP para cédula {cedula}")
        print('='*60)
        r = await client.get(
            f"{BASE}/{DS_CONTRATOS}.json",
            params={"$where": f"documento_proveedor = '{cedula}'", "$limit": "500", "$order": "fecha_de_firma DESC"},
        )
        contratos = r.json()
        if not isinstance(contratos, list):
            print("Error:", contratos)
            return
        print(f"Total contratos Socrata: {len(contratos)}")
        print()

        # Mostrar todos los tipos de contrato y cuáles pasarían el filtro "prestaci"
        tipos = {}
        for c in contratos:
            t = c.get("tipo_de_contrato") or "(sin tipo)"
            tipos[t] = tipos.get(t, 0) + 1
        print("Tipos de contrato encontrados:")
        for t, cnt in sorted(tipos.items(), key=lambda x: -x[1]):
            pasa = "prestaci" in t.lower()
            print(f"  {'OK' if pasa else '--'} [{cnt}] {t}")

        print()
        print("Detalle por contrato:")
        for c in contratos:
            tipo = c.get("tipo_de_contrato", "?")
            proc = c.get("proceso_de_compra", "?")
            ref = c.get("referencia_del_contrato", "?")
            idc = c.get("id_contrato", "?")
            estado = c.get("estado_contrato", "?")
            valor = c.get("valor_del_contrato", "?")
            pasa = "OK" if "prestaci" in tipo.lower() else "--"
            print(f"  {pasa} {idc} | {ref} | tipo={tipo[:30]} | estado={estado} | valor={valor}")

        # ── 2. Documentos para cada contrato
        print(f"\n{'='*60}")
        print("DOCUMENTOS POR CONTRATO EN SECOP")
        print('='*60)
        for c in contratos:
            proc = c.get("proceso_de_compra") or ""
            idc = c.get("id_contrato") or ""
            ref = c.get("referencia_del_contrato") or ""
            print(f"\n  Contrato: {idc} | proceso={proc}")

            total_docs = 0
            for ds_id, label in DS_DOCS:
                where_parts = []
                if proc:
                    where_parts.append(f"proceso = '{proc}'")
                if ref:
                    where_parts.append(f"n_mero_de_contrato = '{ref}'")
                if idc:
                    where_parts.append(f"n_mero_de_contrato = '{idc}'")
                if not where_parts:
                    continue
                where = " OR ".join(where_parts)
                # Count total (sin límite)
                r_count = await client.get(
                    f"{BASE}/{ds_id}.json",
                    params={"$select": "count(*)", "$where": where},
                )
                try:
                    cnt = int(r_count.json()[0].get("count", 0))
                except Exception:
                    cnt = 0
                if cnt > 0:
                    total_docs += cnt
                    print(f"    [{label}] {cnt} docs")

            # Modificaciones
            if idc:
                r_mod = await client.get(
                    f"{BASE}/{DS_MODS}.json",
                    params={"$select": "count(*)", "$where": f"id_contrato = '{idc}'"},
                )
                try:
                    cnt_mod = int(r_mod.json()[0].get("count", 0))
                except Exception:
                    cnt_mod = 0
                if cnt_mod > 0:
                    total_docs += cnt_mod
                    print(f"    [modificaciones] {cnt_mod} docs")

            if total_docs == 0:
                print(f"    (sin documentos)")
            else:
                print(f"    TOTAL: {total_docs} documentos")

        # ── 3. Campos disponibles en el dataset de contratos
        print(f"\n{'='*60}")
        print("TODOS LOS CAMPOS en jbjy-vk9h (contratos)")
        print('='*60)
        meta = (await client.get(f"https://www.datos.gov.co/api/views/{DS_CONTRATOS}.json")).json()
        for col in meta.get("columns", []):
            print(f"  {col['fieldName']:45} ({col.get('dataTypeName','')})")


if __name__ == "__main__":
    cedula = sys.argv[1] if len(sys.argv) > 1 else "1016019452"
    asyncio.run(main(cedula))
