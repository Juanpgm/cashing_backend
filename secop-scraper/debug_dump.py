"""Throwaway debug script: dump the real state of the SECOP II page.

Not part of the service. Run manually to inspect why the scraper reports
captcha_required — could be a real reCAPTCHA wall or a false-positive
detection heuristic.
"""
import asyncio
from playwright.async_api import async_playwright

URL = (
    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
    "Index?noticeUID=CO1.NTC.7092638&isFromPublicArea=True"
)


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="es-CO",
            viewport={"width": 1280, "height": 900},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await ctx.new_page()
        print(f"Navigating to {URL}")
        resp = await page.goto(URL, wait_until="domcontentloaded", timeout=30_000)
        print("HTTP status:", resp.status if resp else None)
        await page.wait_for_timeout(3000)
        title = await page.title()
        print("Title:", title)
        print("Final URL:", page.url)

        recaptcha_iframe = await page.query_selector("iframe[src*='recaptcha']")
        recaptcha_div = await page.query_selector("div.g-recaptcha")
        print("recaptcha iframe present:", bool(recaptcha_iframe))
        print("recaptcha div present:", bool(recaptcha_div))

        body_text = await page.inner_text("body")
        print("Body text length:", len(body_text))
        print("Body text snippet (first 1500 chars):")
        print(body_text[:1500])

        html = await page.content()
        with open("debug_page.html", "w", encoding="utf-8") as f:
            f.write(html)
        await page.screenshot(path="debug_screenshot.png", full_page=True)
        print("Saved debug_page.html and debug_screenshot.png")

        # Try to wait a bit longer + networkidle to see if content loads (SPA)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
            print("networkidle reached")
        except Exception as exc:
            print("networkidle timeout:", exc)

        body_text2 = await page.inner_text("body")
        print("Body text length after networkidle wait:", len(body_text2))
        html2 = await page.content()
        with open("debug_page_after_wait.html", "w", encoding="utf-8") as f:
            f.write(html2)
        await page.screenshot(path="debug_screenshot_after_wait.png", full_page=True)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
