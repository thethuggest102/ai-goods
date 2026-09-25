"""
Fills the Kingoppay hosted checkout in a real browser, the way a person would.

Card fields live in three gateway iframes (CardNumber / CardExpiry / CardCvc),
cardholder name is on the main page. After "Pay" the bank's 3-D Secure step
follows: Kaspi usually sends a push to the app (nothing to type), sometimes an
SMS code — then `ask_otp` is awaited and the code typed into the bank page.

Card data only lives in memory for the duration of one payment and is never logged.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from loguru import logger
from patchright.async_api import Frame, Page, async_playwright

from aigoods_worker.config import AigoodsSettings


@dataclass
class Card:
    """Card details typed by the owner in the admin form. Never persisted."""

    number: str
    expiry: str  # MM/YY
    cvc: str
    holder: str

    def __repr__(self) -> str:  # keep card data out of logs and tracebacks
        return f"Card(****{self.number[-4:]})"


class PayResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"


OtpAsker = Callable[[], Awaitable[str]]
StatusCallback = Callable[[str], None]

_OTP_SELECTORS = "input[autocomplete='one-time-code'], input[name*='otp' i], input[name*='code' i], input[type='password']"


async def pay_checkout(
    settings: AigoodsSettings,
    checkout_url: str,
    card: Card,
    ask_otp: OtpAsker,
    on_status: StatusCallback = lambda s: None,
    timeout_s: int = 300,
) -> PayResult:
    """Open checkout, fill the card, press Pay and follow 3-D Secure until the result page."""
    host, port, user, pwd = settings.proxy.split(":")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=settings.headless,
            proxy={"server": f"http://{host}:{port}", "username": user, "password": pwd},
        )
        try:
            ctx = await browser.new_context(
                locale="ru-RU", timezone_id="Asia/Almaty", viewport={"width": 1280, "height": 900}
            )
            page = await ctx.new_page()
            mouse = _HumanMouse(page)
            on_status("Открываю страницу оплаты")
            await page.goto(checkout_url, wait_until="domcontentloaded", timeout=60_000)
            await _fill_card(page, mouse, card)
            on_status("Карта введена, нажимаю «Pay»")
            await _human_pause()
            await mouse.click(page.get_by_role("button", name="Pay"))
            on_status("Жду подтверждения в Kaspi (пуш в приложении или SMS-код)")
            return await _wait_result(page, ask_otp, on_status, timeout_s)
        finally:
            await browser.close()


class _HumanMouse:
    """Moves the cursor along a curved, jittery path before clicking, like a real hand."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.x = random.uniform(150, 500)
        self.y = random.uniform(150, 400)

    async def _move_to(self, x: float, y: float) -> None:
        steps = random.randint(14, 24)
        cx = (self.x + x) / 2 + random.uniform(-70, 70)  # bezier control point → curve
        cy = (self.y + y) / 2 + random.uniform(-70, 70)
        x0, y0 = self.x, self.y
        for i in range(1, steps + 1):
            t = i / steps
            bx = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t**2 * x
            by = (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t**2 * y
            await self.page.mouse.move(bx + random.uniform(-1, 1), by + random.uniform(-1, 1))
            await asyncio.sleep(random.uniform(0.008, 0.028))
        self.x, self.y = x, y

    async def click(self, locator) -> None:  # type: ignore[no-untyped-def]
        """Move to a point inside the element, then click there."""
        box = await locator.bounding_box()
        if not box:
            await locator.click()
            return
        tx = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        ty = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        await self._move_to(tx, ty)
        await _human_pause(0.05, 0.25)
        await self.page.mouse.click(tx, ty)


async def _fill_card(page: Page, mouse: "_HumanMouse", card: Card) -> None:
    number = await _frame(page, "/CardNumber")
    await _type(mouse, number, "input[name='cardInput']", card.number.replace(" ", ""))
    expiry = await _frame(page, "/CardExpiry")
    await _type(mouse, expiry, "input[name='expiryInput']", card.expiry.replace("/", "").replace(" ", ""))
    cvc = await _frame(page, "/CardCvc")
    await _type(mouse, cvc, "input[name='cvcInput']", card.cvc)
    await _type(mouse, page.main_frame, "input[placeholder='Cardholder name']", card.holder.upper())


async def _frame(page: Page, path_part: str, timeout_s: int = 40) -> Frame:
    for _ in range(timeout_s * 2):
        for f in page.frames:
            if path_part in f.url:
                return f
        await asyncio.sleep(0.5)
    raise TimeoutError(f"card iframe {path_part} did not load")


async def _type(mouse: "_HumanMouse", frame: Frame, selector: str, text: str) -> None:
    field = frame.locator(selector).first
    await field.wait_for(state="visible", timeout=30_000)
    await mouse.click(field)
    await _human_pause(0.2, 0.6)
    await field.press_sequentially(text, delay=random.randint(70, 160))
    await _human_pause()


async def _human_pause(lo: float = 0.4, hi: float = 1.2) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _wait_result(
    page: Page, ask_otp: OtpAsker, on_status: StatusCallback, timeout_s: int
) -> PayResult:
    otp_sent = False
    mouse = _HumanMouse(page)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        url = page.url
        if "ai-goods.eu" in url and "success" in url:
            return PayResult.SUCCESS
        if "ai-goods.eu" in url and "fail" in url:
            return PayResult.FAILED
        if not otp_sent:
            otp_frame = await _find_otp(page)
            if otp_frame is not None:
                on_status("Банк просит код из SMS — введите его в форме")
                code = await ask_otp()
                await _type(mouse, otp_frame, _OTP_SELECTORS, code)
                await otp_frame.locator("button[type='submit'], input[type='submit']").first.click()
                otp_sent = True
                on_status("Код отправлен, жду ответа банка")
        await asyncio.sleep(2)
    logger.warning(f"payment: no result within {timeout_s}s, last url host={page.url.split('/')[2]}")
    return PayResult.TIMEOUT


async def _find_otp(page: Page) -> Frame | None:
    for f in page.frames:
        if "kingoppay.com" in f.url:
            continue
        try:
            if await f.locator(_OTP_SELECTORS).count():
                return f
        except Exception:  # frame detached during navigation
            continue
    return None
