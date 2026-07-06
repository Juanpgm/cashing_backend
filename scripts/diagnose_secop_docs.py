"""Diagnostic: shows what SECOP returns for documents of a given contract.

Usage:
    python scripts/diagnose_secop_docs.py <numero_contrato>

Example:
    python scripts/diagnose_secop_docs.py CO1.PCCNTR.4093155
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

_SECOP_BASE = "https://www.datos.gov.co/resource"
_DS_CONTRATOS = "jbjy-vk9h"
_DS_DOCUMENTOS = "dmgg-8hin"


async def query_socrata(dataset_id: str, where_clause: str, limit: int = 1000) -> list[dict]:
    url = f"{_SECOP_BASE}/{dataset_id}.json"
    params = {"$where": where_clause, "$limit": str(limit)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("results", [])


async def main(numero_contrato: str) -> None:
    print(f"\n{'='*70}")
    print(f"DIAGNÓSTICO SECOP para: {numero_contrato}")
    print(f"{'='*70}\n")

    # 1. Buscar en dataset de contratos por referencia y numero
    print("── 1. Buscando filas en dataset CONTRATOS ──────────────────────────")
    rows_by_ref = await query_socrata(
        _DS_CONTRATOS,
        where_clause=f"referencia_del_contrato = '{numero_contrato}'",
    )
    rows_by_num = await query_socrata(
        _DS_CONTRATOS,
        where_clause=f"numero_contrato = '{numero_contrato}'",
    )
    all_contrato_rows = {r["id_contrato"]: r for r in rows_by_ref + rows_by_num if r.get("id_contrato")}
    print(f"   Por referencia_del_contrato: {len(rows_by_ref)} filas")
    print(f"   Por numero_contrato:          {len(rows_by_num)} filas")
    print(f"   Total único (id_contrato):    {len(all_contrato_rows)} filas\n")

    if not all_contrato_rows:
        print("   ⚠  No se encontró ningún contrato en SECOP con esa referencia.")
        print("   Prueba con el número corto (ej: el entero o CO1.PCCNTR.XXXXXXX)")
        return

    procesos: set[str] = set()
    referencias: set[str] = set()
    referencias.add(numero_contrato)

    for row in all_contrato_rows.values():
        proc = str(row.get("proceso_de_compra") or "").strip()
        ref = str(row.get("referencia_del_contrato") or "").strip()
        num = str(row.get("numero_contrato") or "").strip()
        if proc:
            procesos.add(proc)
        if ref:
            referencias.add(ref)
        if num:
            referencias.add(num)
        print(f"   id_contrato:              {row.get('id_contrato')}")
        print(f"   numero_contrato:          {row.get('numero_contrato')}")
        print(f"   referencia_del_contrato:  {row.get('referencia_del_contrato')}")
        print(f"   proceso_de_compra:        {row.get('proceso_de_compra')}")
        print(f"   estado_contrato:          {row.get('estado_contrato')}")
        print(f"   tipo_de_contrato:         {row.get('tipo_de_contrato')}")
        print()

    print(f"   Referencias a usar para docs: {referencias}")
    print(f"   Procesos a usar para docs:    {procesos}")

    # 2. Buscar documentos por cada referencia
    print("\n── 2. Documentos por n_mero_de_contrato ────────────────────────────")
    all_docs: dict[str, dict] = {}
    for ref in sorted(referencias):
        rows = await query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"n_mero_de_contrato = '{ref.replace(chr(39), chr(39)*2)}'",
        )
        print(f"   [{ref}] → {len(rows)} documentos")
        for r in rows:
            all_docs[r.get("id_documento", "")] = r

    # 3. Buscar documentos por proceso
    print("\n── 3. Documentos por proceso (proceso_de_compra) ───────────────────")
    for proc in sorted(procesos):
        rows = await query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"proceso = '{proc.replace(chr(39), chr(39)*2)}'",
        )
        print(f"   [{proc}] → {len(rows)} documentos")
        for r in rows:
            all_docs[r.get("id_documento", "")] = r

    # 4. Resumen
    print(f"\n── 4. TOTAL documentos únicos: {len(all_docs)} ─────────────────────")
    for doc in sorted(all_docs.values(), key=lambda d: d.get("fecha_carga") or ""):
        ext = doc.get("extensi_n", "?")
        nombre = doc.get("nombre_archivo") or doc.get("descripci_n") or "(sin nombre)"
        fecha = doc.get("fecha_carga", "?")[:10] if doc.get("fecha_carga") else "?"
        proc = doc.get("proceso", "")
        num_c = doc.get("n_mero_de_contrato", "")
        print(f"   [{fecha}] {ext:6} | {nombre[:60]} | contrato={num_c} | proceso={proc}")

    print(f"\n{'='*70}")
    print(f"RESUMEN FINAL: {len(all_docs)} documentos únicos encontrados en SECOP")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/diagnose_secop_docs.py <numero_contrato>")
        print("Ejemplo: python scripts/diagnose_secop_docs.py CO1.PCCNTR.4093155")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
