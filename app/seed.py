"""Idempotent development seed (SPEC SECTION 9, README quickstart).

Ensures the four SYSTEM_* wallets exist. The baseline migration already seeds them,
so this is a safety net for databases created another way; it never duplicates a
system wallet thanks to the partial unique indexes.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import build_engine, build_session_factory
from app.models import Wallet
from app.models.enums import WalletType

_SYSTEM_WALLETS = (
    WalletType.SYSTEM_ESCROW,
    WalletType.SYSTEM_REVENUE,
    WalletType.SYSTEM_GATEWAY,
    WalletType.SYSTEM_TAX_PAYABLE,
)


async def seed_system_wallets() -> int:
    """Create any missing system wallets. Returns the number created."""
    settings = get_settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    created = 0
    async with factory() as session:
        for wallet_type in _SYSTEM_WALLETS:
            existing = await session.scalar(select(Wallet).where(Wallet.type == wallet_type))
            if existing is None:
                session.add(Wallet(type=wallet_type, user_id=None))
                created += 1
        await session.commit()
    await engine.dispose()
    return created


def main() -> None:
    """Run the seed and report how many system wallets were created."""
    created = asyncio.run(seed_system_wallets())
    print(f"Seed complete. Created {created} system wallet(s).")


if __name__ == "__main__":
    main()
