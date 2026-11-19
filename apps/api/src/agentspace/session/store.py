"""Session and transcript persistence on top of the SQLModel tables."""

from pydantic import TypeAdapter
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agentspace.ai.messages import Message
from agentspace.session.models import MessageRow, SessionRow, utcnow

# Turns a stored JSON message back into the right Message class by its `role`.
# TS: like a zod schema for a union type (z.discriminatedUnion); .validate_python() is .parse().
message_adapter: TypeAdapter[Message] = TypeAdapter(Message)


class SessionStore:
    """Reads and writes sessions and their transcripts. Every write commits."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self, *, provider: str, model: str, system_prompt: str, tools: list[str], title: str | None = None
    ) -> SessionRow:
        """Create and save a new, empty session."""
        row = SessionRow(provider=provider, model=model, system_prompt=system_prompt, tools=tools, title=title)
        self.db.add(row)
        await self.db.commit()  # TS: commits the transaction; nothing is written until then
        return row

    async def get(self, session_id: str) -> SessionRow | None:
        """The session with this id, or None."""
        return await self.db.get(SessionRow, session_id)

    async def list_all(self) -> list[SessionRow]:
        """Every session, most recently active first."""
        # TS: a query builder, like Drizzle's db.select().from(sessions).orderBy(desc(sessions.updatedAt)).
        result = await self.db.exec(select(SessionRow).order_by(col(SessionRow.updated_at).desc()))
        return list(result.all())

    async def set_model(self, session: SessionRow, model: str) -> SessionRow:
        """Switch the model the session's next runs use."""
        session.model = model
        self.db.add(session)
        await self.db.commit()
        return session

    async def set_title(self, session: SessionRow, title: str) -> SessionRow:
        """Name the session."""
        session.title = title
        self.db.add(session)
        await self.db.commit()
        return session

    async def messages(self, session_id: str) -> list[Message]:
        """A session's transcript, oldest message first."""
        result = await self.db.exec(
            select(MessageRow).where(MessageRow.session_id == session_id).order_by(col(MessageRow.seq))
        )
        # TS: rows.map((row) => Message.parse(row.data))
        return [message_adapter.validate_python(row.data) for row in result.all()]

    async def append(self, session: SessionRow, message: Message) -> MessageRow:
        """Save a message after the session's last one and bump the session's `updated_at`."""
        last = (
            await self.db.exec(
                select(MessageRow)
                .where(MessageRow.session_id == session.id)
                .order_by(col(MessageRow.seq).desc())
                .limit(1)
            )
        ).first()
        row = MessageRow(
            session_id=session.id,
            parent_id=last.id if last else None,  # TS: last ? last.id : null
            seq=last.seq + 1 if last else 0,
            role=message.role,
            data=message.model_dump(mode="json"),
        )
        session.updated_at = utcnow()
        self.db.add(row)
        self.db.add(session)
        await self.db.commit()
        return row
