"""Manual reverse-engineering tool for SECOP II contract documents.

Run this with `python scripts/_explore_secop2_headed.py CO1.NTC.9506401`
to open a real Chromium window. You solve any captcha manually, then click
the "Documentos del Contrato" tab. The script captures every XHR/fetch and
prints the URL + payload + response — so we can pinpoint which endpoint
returns the contract documents and replicate it programmatically later.

Requires: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import Request, Response, async_playwright

LOG_DIR = Path(__file__).parent / "_secop2_capture"
LOG_DIR.mkdir(exist_ok=True)


async def main(notice_uid: str) -> None:
    target_url = (
        f"https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
        f"Index?noticeUID={notice_uid}&isFromPublicArea=True"
    )
    print(f"\nOpening: {target_url}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=80)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="es-CO",
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        captures: list[dict] = []

        async def on_request(req: Request) -> None:
            if req.resource_type in ("image", "font", "stylesheet"):
                return
            entry = {
                "method": req.method,
                "url": req.url,
                "headers": await req.all_headers(),
                "post_data": req.post_data,
            }
            captures.append(entry)

        async def on_response(resp: Response) -> None:
            url = resp.url
            if any(s in url for s in ("Document", "Contract", "Archive", "Tendering")):
                try:
                    body = await resp.text()
                except Exception:
                    body = "<binary>"
                print(f"\n→ [{resp.status}] {resp.request.method} {url}")
                if body and len(body) < 2000:
                    print(f"   body: {body[:1500]}")
                else:
                    print(f"   body length: {len(body)}")
                # Save to file
                fname = f"{resp.status}_{resp.request.method}_{abs(hash(url)) % 100000}.txt"
                (LOG_DIR / fname).write_text(
                    f"URL: {url}\nMETHOD: {resp.request.method}\nSTATUS: {resp.status}\n\n{body}",
                    encoding="utf-8",
                )

        page.on("request", lambda r: asyncio.create_task(on_request(r)))
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)

        print("\n" + "=" * 70)
        print("BROWSER OPEN. Resolve any captcha, then:")
        print("  1. Click the 'Documentos del Contrato' tab")
        print("  2. Wait until the document list is visible")
        print("  3. Right-click on a document, copy its URL — paste it here later")
        print("  4. Press ENTER in this terminal to dump captures and exit")
        print("=" * 70)

        # Wait for user input in a non-blocking way
        await asyncio.get_event_loop().run_in_executor(None, input)

        # Dump request log
        log_file = LOG_DIR / f"requests_{notice_uid}.json"
        log_file.write_text(json.dumps(captures, indent=2, default=str), encoding="utf-8")
        print(f"\nSaved {len(captures)} requests → {log_file}")

        # Save final cookies for reuse
        storage = await context.storage_state()
        (LOG_DIR / "storage_state.json").write_text(json.dumps(storage), encoding="utf-8")
        print(f"Saved session storage → {LOG_DIR / 'storage_state.json'}")

        # Save final HTML
        html = await page.content()
        (LOG_DIR / "final_page.html").write_text(html, encoding="utf-8")
        print(f"Saved final HTML → {LOG_DIR / 'final_page.html'}")

        await browser.close()


if __name__ == "__main__":
    uid = sys.argv[1] if len(sys.argv) > 1 else "CO1.NTC.9506401"
    asyncio.run(main(uid))
