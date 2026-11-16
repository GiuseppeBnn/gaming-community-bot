"""Public gift card; mentions are pre-escaped HTML, item/AI text is always escaped."""

from __future__ import annotations

from services.catalog_loader import ConsumableItem
from utils.text import esc


def public_offer_text(
    sender: str, recipient: str, item: ConsumableItem | None,
    *, consumed: bool = False, consumer: str | None = None, reaction: str | None = None,
) -> str:
    label = f"{esc(item.emoji)} {esc(item.name)}" if item else "un oggetto"
    lines = [f"🎁 <b>Un'offerta per {recipient}</b>", f"Da {sender}: <b>{label}</b>."]
    if consumed:
        lines += ["", f"🍽 <b>Consumato da {consumer}.</b>" if consumer else "🍽 <b>Regalo già consumato.</b>"]
        if reaction:
            lines.append(f"🐲 <i>{esc(reaction)}</i>")
    else:
        lines += ["", f"Il regalo è nella dispensa di {recipient}.",
                  "Solo il destinatario può scegliere se consumarlo ora o conservarlo."]
    return "\n".join(lines)
