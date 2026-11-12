"""Admin smoke command, using the exact same renderer and budget as the relay."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from config_data.config import settings
from filters.admin_filter import IsAdminFilter
from services import admin_service, ai_budget, image_service

router = Router(name="images")
router.message.filter(IsAdminFilter())
log = logging.getLogger(__name__)
# A UX guard only; the SQL ledger remains the authority on spending. No await
# separates check/add, and finally frees the slot even on cancellation.
_inflight: set[int] = set()
_USAGE = (
    "Uso: <code>/immagine descrizione</code>\n"
    "Da 1 a 150 caratteri, come nella staffetta.\n"
    "Esempio: <code>/immagine un pinguino che vende gelati sulla luna</code>"
)


@router.message(Command("immagine", ignore_case=True))
async def cmd_immagine(
    message: Message,
    command: CommandObject,
    db_session: AsyncSession,
) -> None:
    prompt = (command.args or "").strip()
    if not 1 <= len(prompt) <= 150:
        await db_session.rollback()
        await message.answer(_USAGE)
        return
    if not settings.telephone_enabled or not settings.openrouter_api_key:
        await db_session.rollback()
        await message.answer("Generazione immagini non abilitata o OpenRouter non configurato.")
        return
    uid = message.from_user.id
    if uid in _inflight:
        await db_session.rollback()
        await message.answer("🎨 Hai già un'immagine in preparazione. Attendi il risultato.")
        return
    _inflight.add(uid)
    phase = "audit"
    try:
        await admin_service.log_action(
            db_session,
            uid,
            "image_generate",
            detail=f"model={settings.telephone_image_model}; prompt_chars={len(prompt)}",
        )
        await db_session.commit()  # no handler transaction over HTTP/Telegram
        phase = "ack"
        await message.answer("🎨 Creo l'immagine…", request_timeout=15)
        phase = "generation"
        image = await image_service.generate(
            prompt,
            model=settings.telephone_image_model,
            provider=settings.telephone_image_provider,
            cap_microusd=ai_budget.usd_to_microusd(settings.telephone_image_max_usd),
            feature="admin_image",
        )
        phase = "upload"
        await message.answer_photo(
            BufferedInputFile(image, filename="immagine.png"),
            caption="🖼️ Immagine generata",
            protect_content=True,
            request_timeout=20,
        )
    except ai_budget.AIBudgetExceeded:
        await message.answer("Budget AI mensile esaurito: nessuna nuova immagine richiesta.")
    except ai_budget.AIBudgetError:
        await message.answer(
            "Contabilità AI non disponibile: operazione interrotta, nessun retry automatico."
        )
    except image_service.ImageError as exc:
        log.warning(
            "Admin image failed stage=%s kind=%s http_status=%s",
            exc.stage,
            exc.kind,
            exc.http_status,
        )
        status = f" · HTTP {exc.http_status}" if exc.http_status is not None else ""
        await message.answer(
            f"⚠️ Immagine non riuscita: {exc.stage}/{exc.kind}{status}.\n"
            "Nessun retry automatico. Puoi avviare una nuova richiesta con /immagine.",
            parse_mode=None,
        )
    except TelegramAPIError:
        log.warning("Admin image Telegram failure stage=%s", phase)
        await message.answer(
            "Immagine generata, ma invio Telegram fallito. Non la rigenero automaticamente."
            if phase == "upload"
            else "Telegram non raggiungibile: generazione non avviata."
        )
    except Exception as exc:  # noqa: BLE001 — never expose provider/SQL bodies
        await db_session.rollback()
        log.warning("Admin image stopped stage=%s error=%s", phase, type(exc).__name__)
        await message.answer(
            "Operazione interrotta. Nessun retry automatico; controlla i log diagnostici."
        )
    finally:
        _inflight.discard(uid)
