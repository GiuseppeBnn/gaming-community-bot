"""Persistent gift confirmations; inventory and payment share one transaction.

No Telegram, AI or commits here. Gift roots are locked before accounts and units.
The receipt remains usable after a restart or a failed Telegram announcement.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import TavernOffer, TransactionType, User
from services import consumable_service, economy_service
from utils.daytime import utc_now

OFFER_TTL = timedelta(minutes=10)
OFFER_COOLDOWN = timedelta(seconds=60)
MAX_PENDING_OFFERS = 5
PUBLISH_LEASE = timedelta(seconds=60)


class TavernError(ValueError):
    """Safe, local domain feedback; never wraps provider or database bodies."""


@dataclass(frozen=True)
class Offer:
    id: int
    token: str
    sender_tg_id: int
    recipient_tg_id: int
    group_id: int
    request_message_id: int
    status: str
    item_key: str | None
    source_purchase_id: int | None
    cost: int
    purchase_id: int | None
    created_at: datetime
    expires_at: datetime
    delivered_at: datetime | None
    publish_lease_until: datetime | None
    message_id: int | None


async def get_offer(session: AsyncSession, offer_id: int, *, lock: bool = False) -> Offer:
    stmt = select(TavernOffer.__table__).where(TavernOffer.id == offer_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        raise TavernError("Questa offerta non esiste più.")
    return Offer(**row)


async def find_offer(session: AsyncSession, token: str, sender_tg_id: int) -> Offer:
    offer_id = (await session.execute(
        select(TavernOffer.id).where(
            TavernOffer.token == token, TavernOffer.sender_tg_id == sender_tg_id,
        )
    )).scalar_one_or_none()
    if offer_id is None:
        raise TavernError("Questo invito è personale o non è più valido.")
    return await get_offer(session, offer_id)


def authorize_sender(offer: Offer, sender_tg_id: int, group_id: int) -> None:
    if offer.sender_tg_id != sender_tg_id or offer.group_id != group_id or not group_id:
        raise TavernError("Questa offerta non è tua o appartiene a un altro gruppo.")


def _pending(offer: Offer, status: str) -> None:
    if offer.status != status or utc_now() >= offer.expires_at:
        raise TavernError("Offerta scaduta o già gestita. Puoi ripartire con /offri.")


async def create_offer(
    session: AsyncSession, sender_tg_id: int, recipient_tg_id: int,
    group_id: int, request_message_id: int,
) -> Offer:
    if not group_id or sender_tg_id == recipient_tg_id:
        raise TavernError("Scegli un altro membro del gruppo a cui offrire qualcosa.")
    # A sender lock also serializes pending-offer limits and duplicate updates.
    sender = (await session.execute(
        select(User.tg_id).where(User.tg_id == sender_tg_id).with_for_update()
    )).scalar_one_or_none()
    recipient = (await session.execute(
        select(User.tg_id).where(User.tg_id == recipient_tg_id)
    )).scalar_one_or_none()
    if sender is None or recipient is None:
        raise TavernError("Entrambi dovete prima registrarvi con /start.")
    existing = (await session.execute(
        select(TavernOffer.id).where(
            TavernOffer.group_id == group_id,
            TavernOffer.request_message_id == request_message_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        offer = await get_offer(session, existing)
        authorize_sender(offer, sender_tg_id, group_id)
        return offer
    now = utc_now()
    pending = (await session.execute(
        select(func.count()).select_from(TavernOffer).where(
            TavernOffer.sender_tg_id == sender_tg_id,
            TavernOffer.status.in_(("draft", "prepared")), TavernOffer.expires_at > now,
        )
    )).scalar_one()
    if pending >= MAX_PENDING_OFFERS:
        raise TavernError("Hai già 5 offerte aperte: completale o annullale dalla Locanda.")
    row = TavernOffer(
        token=secrets.token_urlsafe(16), sender_tg_id=sender_tg_id,
        recipient_tg_id=recipient_tg_id, group_id=group_id,
        request_message_id=request_message_id, status="draft", cost=0,
        created_at=now, expires_at=now + OFFER_TTL,
    )
    session.add(row)
    await session.flush()
    return await get_offer(session, row.id)


async def prepare_offer(
    session: AsyncSession, offer_id: int, sender_tg_id: int, group_id: int,
    *, item_key: str | None = None, source_purchase_id: int | None = None,
) -> Offer:
    offer = await get_offer(session, offer_id, lock=True)
    authorize_sender(offer, sender_tg_id, group_id)
    _pending(offer, "draft")
    if (item_key is None) == (source_purchase_id is None):
        raise TavernError("Scegli un oggetto dal menù o dalla tua dispensa.")
    if source_purchase_id is not None:
        units = await consumable_service.stock_units(session, sender_tg_id)
        item_key = next((key for key, unit in units.items() if unit == source_purchase_id), None)
    item = consumable_service.get_item(item_key) if item_key else None
    if item is None or item.price <= 0:
        raise TavernError("Oggetto non disponibile. Riapri la tua offerta dalla Locanda.")
    await session.execute(
        update(TavernOffer).where(TavernOffer.id == offer_id, TavernOffer.status == "draft")
        .values(status="prepared", item_key=item.key, source_purchase_id=source_purchase_id,
                cost=item.price if source_purchase_id is None else 0)
        .execution_options(synchronize_session=False)
    )
    return await get_offer(session, offer_id)


async def cancel_offer(
    session: AsyncSession, offer_id: int, sender_tg_id: int, group_id: int,
) -> bool:
    offer = await get_offer(session, offer_id, lock=True)
    authorize_sender(offer, sender_tg_id, group_id)
    result = await session.execute(
        update(TavernOffer).where(
            TavernOffer.id == offer_id, TavernOffer.status.in_(("draft", "prepared")),
        ).values(status="cancelled").execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def deliver_offer(
    session: AsyncSession, offer_id: int, sender_tg_id: int, group_id: int,
) -> tuple[Offer, bool]:
    offer = await get_offer(session, offer_id, lock=True)
    authorize_sender(offer, sender_tg_id, group_id)
    if offer.status == "delivered":
        return offer, False
    _pending(offer, "prepared")
    await economy_service.lock_users_then_wallets(
        session, (sender_tg_id, offer.recipient_tg_id),
    )
    # Recheck the deadline AFTER account-lock waits, not at request arrival.
    _pending(offer, "prepared")
    now = utc_now()
    last = (await session.execute(
        select(func.max(TavernOffer.delivered_at)).where(
            TavernOffer.sender_tg_id == sender_tg_id, TavernOffer.status == "delivered",
        )
    )).scalar_one()
    if last is not None and now < last + OFFER_COOLDOWN:
        raise TavernError("Puoi fare un'offerta al minuto. Riprova tra poco.")
    # Accounts can disappear between the command and confirmation.
    recipient = (await session.execute(
        select(User.tg_id).where(User.tg_id == offer.recipient_tg_id)
    )).scalar_one_or_none()
    if recipient is None:
        raise TavernError("Il destinatario non è più registrato.")
    item = consumable_service.get_item(offer.item_key) if offer.item_key else None
    if item is None:
        raise TavernError("Questo oggetto non è più nel menù.")
    purchase_id = offer.source_purchase_id
    if purchase_id is None:
        if item.price != offer.cost or offer.cost <= 0:
            raise TavernError("Il prezzo è cambiato: annulla e crea una nuova offerta.")
        await economy_service.debit(
            session, sender_tg_id, offer.cost, TransactionType.shop_purchase,
            f"Locanda: offerta #{offer.id} di {item.name}",
        )
        purchase = await consumable_service.record_consumption(session, sender_tg_id, item, offer.cost)
        await session.flush()
        purchase_id = purchase.id
    if not await consumable_service.move_unit(session, purchase_id, sender_tg_id, offer.recipient_tg_id):
        raise TavernError("Quell'oggetto non è più nella tua dispensa: nessun addebito.")
    await session.execute(
        update(TavernOffer).where(TavernOffer.id == offer_id, TavernOffer.status == "prepared")
        .values(status="delivered", purchase_id=purchase_id, delivered_at=now)
        .execution_options(synchronize_session=False)
    )
    return await get_offer(session, offer_id), True


async def recent_offers(session: AsyncSession, sender_tg_id: int) -> list[Offer]:
    rows = (await session.execute(
        select(TavernOffer.__table__).where(TavernOffer.sender_tg_id == sender_tg_id)
        .order_by(TavernOffer.id.desc()).limit(10)
    )).mappings().all()
    return [Offer(**row) for row in rows]


async def claim_publication(session: AsyncSession, offer_id: int, sender_tg_id: int, group_id: int) -> bool:
    # Compute the lease AFTER waiting for the root, otherwise a blocked claim
    # could commit a lease that has already expired before the network starts.
    await session.execute(select(TavernOffer.id).where(TavernOffer.id == offer_id).with_for_update())
    now = utc_now()
    result = await session.execute(
        update(TavernOffer).where(
            TavernOffer.id == offer_id, TavernOffer.sender_tg_id == sender_tg_id,
            TavernOffer.group_id == group_id, TavernOffer.status == "delivered",
            TavernOffer.message_id.is_(None),
            or_(TavernOffer.publish_lease_until.is_(None), TavernOffer.publish_lease_until <= now),
        ).values(publish_lease_until=now + PUBLISH_LEASE)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def finish_publication(
    session: AsyncSession, offer_id: int, lease: datetime, message_id: int | None,
) -> None:
    # Lease fencing prevents a late request from overwriting a newer publisher.
    await session.execute(
        update(TavernOffer).where(
            TavernOffer.id == offer_id, TavernOffer.publish_lease_until == lease,
            TavernOffer.message_id.is_(None),
        ).values(message_id=message_id, publish_lease_until=None)
        .execution_options(synchronize_session=False)
    )
