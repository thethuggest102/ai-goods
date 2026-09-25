"""
Manual CLI for ai-goods.eu.

    python -m aigoods_worker status
    python -m aigoods_worker generate "anime red cat in samurai armor"
"""

import asyncio
import sys

from aigoods_worker.client import AigoodsClient
from aigoods_worker.config import AigoodsSettings


async def status(c: AigoodsClient) -> None:
    """Print balance, generation price, allowed formats/sizes and recent deposits."""
    await c.ensure_logged_in()
    st = await c.get_settings()
    print(f"Balance:          €{await c.get_balance_eur():.2f}")
    print(f"Generation cost:  €{st['image_generation_cost_eur']}")
    print(f"Min deposit:      €{st['min_deposit_amount_eur']}")
    print(f"Formats / sizes:  {st.get('formats')} / {st.get('sizes')}")
    for d in (await c.get_deposit_history())[:5]:
        print(f"  deposit {d['order_number']}  €{d['sum']}  {d['status']}  {d['date']}")


async def main() -> None:
    """Dispatch CLI command."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    async with AigoodsClient(AigoodsSettings()) as c:
        if cmd == "status":
            await status(c)
        elif cmd == "generate" and len(sys.argv) > 2:
            print(await c.generate_image(sys.argv[2]))
        else:
            print(__doc__)


if __name__ == "__main__":
    asyncio.run(main())
