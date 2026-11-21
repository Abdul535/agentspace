"""Users' own model API keys, encrypted for their owner (ADR-026)."""

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from agentspace.encryption import SecretBox
from agentspace.session.models import LOCAL_USER_ID, ProviderKeyRow, UserRow, utcnow


async def list_keys(db: AsyncSession, user_id: str) -> list[ProviderKeyRow]:
    """Every key the user has saved, one per provider."""
    rows = await db.exec(select(ProviderKeyRow).where(ProviderKeyRow.user_id == user_id))
    return list(rows.all())


async def save_key(db: AsyncSession, box: SecretBox, user_id: str, provider: str, api_key: str) -> ProviderKeyRow:
    """Add or replace the user's key for the provider, encrypted."""
    row = await db.get(ProviderKeyRow, (user_id, provider))
    sealed = box.encrypt(user_id, api_key)
    if row is None:
        row = ProviderKeyRow(user_id=user_id, provider=provider, api_key=sealed, last4=api_key[-4:])
    else:
        row.api_key, row.last4, row.updated_at = sealed, api_key[-4:], utcnow()
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def seed_local_key(db: AsyncSession, box: SecretBox, provider: str, api_key: str) -> bool:
    """Give the local user the server's key from `.env` if they have none yet; True if it was copied."""
    if await db.get(ProviderKeyRow, (LOCAL_USER_ID, provider)) is not None:
        return False
    if await db.get(UserRow, LOCAL_USER_ID) is None:
        db.add(UserRow(id=LOCAL_USER_ID))
        # The key row's foreign key needs the user saved first.
        await db.flush()
    await save_key(db, box, LOCAL_USER_ID, provider, api_key)
    return True
