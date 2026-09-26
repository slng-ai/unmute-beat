"""A support desk that resolves and reopens tickets, on core's public boundary alone: the reuse proof of session 4b.

Nothing here is a booking: an account, a ticket and its revision, and a
resolution in the caller's words. No phone, no date, no appointment, no slot.
The application writes what the clinic and the salon write, and nothing
more: its state (`DeskState`), the arguments and receipts of its actions, a
small durable backend (`Desk`, stdlib SQLite) and plain typed functions.
`agent.yaml` connects them; core runs them through the same loader,
operation boundary and turn engine.

The backend keeps what core needs from any backend for an effect to be safe
to retry: the intent it was told about before dispatch (`note_intent`), and
each effect filed under its intent id in the same transaction as the change,
so the same intent sent twice changes the ticket once, and a fresh process
can find out what an earlier one did (`unresolved`, `find_effect`).
Faro's store is the pattern: WAL, `synchronous=FULL`, `BEGIN IMMEDIATE`.

`Desk` also carries test switches for the faults of stage B: an answer lost
after the commit, a process that dies after (or before) the commit, a reply
about another ticket, and a concurrent edit that makes a revision stale.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core import Intent

type AccountId = Annotated[str, StringConstraints(pattern=r'^ACC-\d{4}$')]
type TicketId = Annotated[str, StringConstraints(pattern=r'^T-\d{3}$')]


class DeskState(BaseModel):
    """What the desk knows about one call. Every field has a default: a call starts empty."""

    model_config = ConfigDict(extra='forbid')

    account_id: AccountId | None = Field(None, description='The account number, like ACC-1234.')
    ticket_id: TicketId | None = Field(None, description='The ticket number, like T-101.')
    resolution: str = Field('', description='How the caller says the problem was fixed, in their own words.')
    revision: int = 0
    """The ticket's revision as the backend last said it. Only a receipt sets it."""
    ticket_status: str = ''


class TicketRef(BaseModel):
    account_id: AccountId
    ticket_id: TicketId


class Found(BaseModel):
    account_id: str
    ticket_id: str
    revision: int
    status: str
    found: bool


class Resolve(BaseModel):
    account_id: AccountId
    ticket_id: TicketId
    revision: int = Field(ge=1)
    resolution: Annotated[str, StringConstraints(min_length=1)]


class Reopen(BaseModel):
    account_id: AccountId
    ticket_id: TicketId
    revision: int = Field(ge=1)


class Changed(BaseModel):
    """What the desk did to a ticket: the revision it acted on and the one it is at now."""

    account_id: str
    ticket_id: str
    revision: int
    now_revision: int
    status: Literal['resolved', 'open', 'stale']


class Desk:
    """One support desk's database, opened per call with the session it serves. Every method is one transaction."""

    def __init__(self, path: str | Path, session: str = 'S1') -> None:
        self.path, self.session = str(path), session
        self.lose_answer = False
        """Commit the change, then fail to answer, as a timeout would."""
        self.die_after_commit = False
        """Commit the change, then kill the process before anything answers."""
        self.die_before_commit = False
        self.wrong_ticket = False
        """Answer about a different ticket than the one asked about."""
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tickets (id TEXT PRIMARY KEY, account TEXT, revision INTEGER, status TEXT);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, session TEXT, operation TEXT, args TEXT, subject TEXT);
                CREATE TABLE IF NOT EXISTS effects (intent TEXT PRIMARY KEY, result TEXT);
                CREATE TABLE IF NOT EXISTS sent (intent TEXT, at INTEGER);
                """
            )
        finally:
            db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    def seed(self, account: str, ticket: str, revision: int = 1, status: str = 'open') -> None:
        with self._tx() as db:
            db.execute(
                'INSERT OR REPLACE INTO tickets VALUES (?, ?, ?, ?)', (ticket, account, revision, status)
            )

    def edit(self, ticket: str) -> None:
        """Somebody else changed the ticket: its revision moves on."""
        with self._tx() as db:
            db.execute('UPDATE tickets SET revision = revision + 1 WHERE id = ?', (ticket,))

    def ticket(self, ticket: str) -> tuple[int, str] | None:
        with self._tx() as db:
            row = db.execute('SELECT revision, status FROM tickets WHERE id = ?', (ticket,)).fetchone()
        return (row[0], row[1]) if row else None

    def sent(self) -> int:
        """How many dispatches reached the database and committed, over every process."""
        with self._tx() as db:
            return db.execute('SELECT COUNT(*) FROM sent').fetchone()[0]

    async def note_intent(self, intent: Intent) -> None:
        """Core's durable record of an intent, before it is dispatched. The same id for other arguments is refused."""
        await asyncio.to_thread(self._note, intent)

    def _note(self, intent: Intent) -> None:
        with self._tx() as db:
            db.execute(
                'INSERT OR IGNORE INTO intents VALUES (?, ?, ?, ?, ?)',
                (intent.id, self.session, intent.operation, intent.args, intent.subject),
            )
            held = db.execute(
                'SELECT operation, args, subject FROM intents WHERE id = ?', (intent.id,)
            ).fetchone()
        if tuple(held) != (intent.operation, intent.args, intent.subject):
            raise ValueError(f'intent {intent.id} is already recorded for different arguments')

    def unresolved(self) -> list[Intent]:
        """Every intent this session recorded before dispatch: what a fresh process reconciles first.

        With an effect filed or without one: the process that was waiting for
        the answer is gone, so whether the caller was told is unknown, and
        reconciliation (`find_effect`) says which ones landed.

        ponytail: every intent of the session comes back. A backend that
        learns when a receipt was delivered would hand back only the rest.
        """
        with self._tx() as db:
            rows = db.execute(
                'SELECT id, operation, args, subject FROM intents WHERE session = ?', (self.session,)
            ).fetchall()
        return [
            Intent(id=i, operation=op, args=args, subject=subject, status='dispatched')
            for i, op, args, subject in rows
        ]

    def effect(self, intent: str) -> dict | None:
        with self._tx() as db:
            row = db.execute('SELECT result FROM effects WHERE intent = ?', (intent,)).fetchone()
        return json.loads(row[0]) if row else None

    def write(self, intent: Intent, to: Literal['resolved', 'open'], args: Resolve | Reopen) -> dict:
        """Change the ticket, once per intent: the change and its effect record commit together, or neither does."""
        with self._tx() as db:
            db.execute('INSERT INTO sent VALUES (?, 0)', (intent.id,))
            if (
                done := db.execute('SELECT result FROM effects WHERE intent = ?', (intent.id,)).fetchone()
            ) is not None:
                return json.loads(done[0])
            held = db.execute('SELECT args FROM intents WHERE id = ?', (intent.id,)).fetchone()
            if held is not None and held[0] != args.model_dump_json():
                raise ValueError(f'intent {intent.id} was recorded for different arguments')
            row = db.execute(
                'SELECT revision FROM tickets WHERE id = ? AND account = ?', (args.ticket_id, args.account_id)
            ).fetchone()
            if row is None or row[0] != args.revision:
                result = {
                    'account_id': args.account_id,
                    'ticket_id': args.ticket_id,
                    'revision': args.revision,
                    'now_revision': row[0] if row else 0,
                    'status': 'stale',
                }
            else:
                db.execute(
                    'UPDATE tickets SET status = ?, revision = revision + 1 WHERE id = ?',
                    (to, args.ticket_id),
                )
                result = {**args.model_dump(include={'account_id', 'ticket_id', 'revision'}), 'status': to}
                result['now_revision'] = args.revision + 1
            db.execute('INSERT INTO effects VALUES (?, ?)', (intent.id, json.dumps(result)))
            if self.die_before_commit:
                os._exit(4)
        if self.die_after_commit:
            os._exit(3)
        if self.lose_answer:
            self.lose_answer = False
            raise TimeoutError('the desk did not answer')
        if self.wrong_ticket:
            return {**result, 'ticket_id': 'T-999'}
        return result


def look_up(desk: Desk, ref: TicketRef) -> Found:
    """The ticket's current revision and status, for this account. A read: no read-back, nothing changes."""
    with desk._tx() as db:
        row = db.execute(
            'SELECT revision, status FROM tickets WHERE id = ? AND account = ?',
            (ref.ticket_id, ref.account_id),
        ).fetchone()
    return Found(
        account_id=ref.account_id,
        ticket_id=ref.ticket_id,
        revision=row[0] if row else 0,
        status=row[1] if row else 'none',
        found=row is not None,
    )


def resolve(desk: Desk, args: Resolve, intent: Intent) -> Changed:
    return Changed.model_validate(desk.write(intent, 'resolved', args))


def reopen(desk: Desk, args: Reopen, intent: Intent) -> Changed:
    return Changed.model_validate(desk.write(intent, 'open', args))


def find_effect(desk: Desk, args: BaseModel, intent: Intent) -> dict | None:
    """What an intent did, by its id: None when the desk has no record of it, which is not a refusal."""
    return desk.effect(intent.id)


def resolve_words(args: Resolve) -> str:
    return f'¿Marco el ticket {args.ticket_id} como resuelto: {args.resolution.strip().rstrip(".")}?'


def reopen_words(args: Reopen) -> str:
    return f'¿Reabro el ticket {args.ticket_id}?'
