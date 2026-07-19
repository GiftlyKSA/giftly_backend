"""CLI to rotate field-encryption keys (SPEC SECTION 17.1, 21).

Run after adding a new key version to ``FIELD_ENCRYPTION_KEYS`` and bumping
``FIELD_ENCRYPTION_KEY_VERSION``: this re-encrypts the mutable Restricted columns to the
new active version. ``messages.content`` is append-only and is never rotated, so the old
key must stay in the map while any message still references it.

    uv run python -m app.rotate_keys
"""

from __future__ import annotations

import asyncio

from app.core.config import get_settings
from app.core.db import build_engine, build_session_factory
from app.services.key_rotation_service import KeyRotationService


async def _run() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            report = await KeyRotationService(session=session, settings=settings).rotate()
            await session.commit()
    finally:
        await engine.dispose()
    print(
        "Key rotation complete: "
        f"national_id={report.courier_national_id}, "
        f"passport_id={report.courier_passport_id}, "
        f"preview={report.conversation_preview}, "
        f"iban={report.withdrawal_iban} (total {report.total})."
    )


def main() -> None:
    """Run the key-rotation job."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
