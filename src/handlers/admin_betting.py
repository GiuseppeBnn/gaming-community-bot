"""
Admin-only betting management panel.

/gestisci_scommesse — entry point (private only; redirects from group)

Callback flow:
  admin_bet:list                          → refresh event list
  admin_bet:event:<id>                    → event detail + action menu
  admin_bet:lock:<id>                     → lock confirmation screen
  admin_bet:confirm_lock:<id>             → execute lock
  admin_bet:resolve:<id>                  → pick winning option
  admin_bet:pick_winner:<id>:<opt_id>     → payout preview + confirm
  admin_bet:confirm_resolve:<id>:<opt_id> → execute resolve, notify affected users
  admin_bet:cancel:<id>                   → cancel confirmation screen
  admin_bet:confirm_cancel:<id>           → execute cancel, notify all bettors
  admin_bet:close                         → delete the panel message

Non-admin users who somehow reach these buttons get a polite denial.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters.command import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from exceptions.economy import EventAlreadySettledError, EventNotFoundError
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers._trophy_announce import announce_trophies
from keyboards.admin_betting_kb import (
    get_admin_confirm_cancel_keyboard,
    get_admin_confirm_lock_keyboard,
    get_admin_confirm_resolve_keyboard,
    get_admin_event_menu_keyboard,
    get_admin_events_keyboard,
    get_admin_resolve_options_keyboard,
)
from services import badge_service, bet_service
from utils.text import esc

router = Router()
# 100%-admin router: gate every message/callback at the root (STEERING §8). The
# per-handler filters + `admin_bet:` deny catch-all stay as defense in depth.
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

_STATUS_LABEL = {
    "open": "🟢 Aperta",
    "locked": "🔒 Bloccata",
    "resolved": "🏁 Risolta",
    "cancelled": "❌ Annullata",
}


# ---------------------------------------------------------------------------
# Entry point command
# ---------------------------------------------------------------------------

@router.message(Command("gestisci_scommesse"), IsAdminFilter())
async def cmd_gestisci_scommesse(
    message: Message, db_session: AsyncSession
) -> None:
    if message.chat.type != "private":
        bot_info = await message.bot.get_me()
        await message.reply(
            "🔐 Gestisci le scommesse dalla chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="⚙️ Apri pannello admin",
                    url=f"https://t.me/{bot_info.username}?start=manage_bets",
                )
            ]]),
        )
        return

    await _show_event_list(message, db_session)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _show_event_list(
    target: Message | CallbackQuery,
    db_session: AsyncSession,
) -> None:
    """Render the panel: edit it in place when it came from a button, post a new one
    when it came from a command or a deep link.

    There used to be an `edit` flag here, but it was never free: every caller passed
    `True` with a callback and `False` with a message, so it only ever restated the
    type. The callback+`edit=False` combination it made expressible had no caller and
    could not be reached.
    """
    events = await bet_service.get_all_active_events(db_session)

    text = (
        "📋 <b>Gestione Scommesse</b>\n\nSeleziona un evento per gestirlo:"
        if events
        else "📋 <b>Gestione Scommesse</b>\n\n<i>Nessun evento attivo al momento.</i>"
    )
    markup = get_admin_events_keyboard(events) if events else InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Chiudi", callback_data="admin_bet:close")]]
    )

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _show_event_menu(
    callback: CallbackQuery,
    db_session: AsyncSession,
    event_id: int,
) -> None:
    event = await bet_service.get_event_detail(db_session, event_id)
    if event is None:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return

    pending_count = sum(1 for b in event.user_bets if b.status == "pending")
    total_pool = sum(o.total_wagered for o in event.options)
    opts_text = "\n".join(
        f"  {'🔹' if i == 0 else '🔸'} <b>{esc(o.label)}</b> — {o.total_wagered} 🪙"
        for i, o in enumerate(event.options)
    )
    status_label = _STATUS_LABEL.get(event.status, event.status)

    await callback.message.edit_text(
        f"🎯 <b>#{event.id} {esc(event.title)}</b>\n\n"
        f"<i>{esc(event.description)}</i>\n\n"
        f"<b>Stato:</b> {status_label}\n"
        f"<b>Opzioni:</b>\n{opts_text}\n\n"
        f"💰 <b>Pool totale:</b> {total_pool} 🪙\n"
        f"🎫 <b>Scommesse attive:</b> {pending_count}\n\n"
        f"Cosa vuoi fare?",
        reply_markup=get_admin_event_menu_keyboard(event_id, event.status),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "admin_bet:list", IsAdminCallbackFilter())
async def cb_admin_list(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_event_list(callback, db_session)


@router.callback_query(F.data.startswith("admin_bet:event:"), IsAdminCallbackFilter())
async def cb_admin_event(callback: CallbackQuery, db_session: AsyncSession) -> None:
    event_id = int(callback.data.split(":")[2])
    await _show_event_menu(callback, db_session, event_id)


# --- LOCK ---

@router.callback_query(F.data.startswith("admin_bet:lock:"), IsAdminCallbackFilter())
async def cb_admin_lock(callback: CallbackQuery, db_session: AsyncSession) -> None:
    event_id = int(callback.data.split(":")[2])
    event = await bet_service.get_event_detail(db_session, event_id)
    if event is None:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return

    pending_count = sum(1 for b in event.user_bets if b.status == "pending")

    await callback.message.edit_text(
        f"🔒 <b>Blocca scommesse — #{event.id} {esc(event.title)}</b>\n\n"
        f"🎫 Scommesse attive: <b>{pending_count}</b>\n\n"
        f"Bloccando l'evento non sarà più possibile piazzare nuove scommesse. "
        f"Le scommesse già piazzate restano valide.\n\n"
        f"Confermi?",
        reply_markup=get_admin_confirm_lock_keyboard(event_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_bet:confirm_lock:"), IsAdminCallbackFilter())
async def cb_admin_confirm_lock(callback: CallbackQuery, db_session: AsyncSession) -> None:
    event_id = int(callback.data.split(":")[2])

    try:
        await bet_service.lock_event(db_session, event_id)
        # Manual lock pre-empts the timed auto-lock: drop its pending task.
        await bet_service.cancel_pending_close(db_session, event_id)
        await db_session.commit()
    except EventNotFoundError:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return
    except EventAlreadySettledError as e:
        await callback.answer(
            f"⚠️ Evento già chiuso (stato: {e.status}).", show_alert=True
        )
        return

    await callback.message.edit_text(
        f"🔒 <b>Evento #{event_id} bloccato.</b>\n\n"
        f"Non sono più accettate nuove scommesse. "
        f"Quando sei pronto, torna al pannello per dichiarare il vincitore.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Torna alla lista", callback_data="admin_bet:list"),
            InlineKeyboardButton(text="✖ Chiudi", callback_data="admin_bet:close"),
        ]]),
    )
    await callback.answer("🔒 Bloccato!")


# --- RESOLVE ---

@router.callback_query(F.data.startswith("admin_bet:resolve:"), IsAdminCallbackFilter())
async def cb_admin_resolve(callback: CallbackQuery, db_session: AsyncSession) -> None:
    event_id = int(callback.data.split(":")[2])
    event = await bet_service.get_event_detail(db_session, event_id)
    if event is None:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return
    if event.status in ("resolved", "cancelled"):
        await callback.answer("⚠️ Evento già chiuso.", show_alert=True)
        return

    total_pool = sum(o.total_wagered for o in event.options)

    await callback.message.edit_text(
        f"🏁 <b>Dichiara vincitore — #{event.id} {esc(event.title)}</b>\n\n"
        f"💰 <b>Pool totale:</b> {total_pool} 🪙\n\n"
        f"Seleziona l'opzione vincente.\n"
        f"<i>Il pool intero sarà distribuito ai vincitori in proporzione al loro bet (stile Twitch).</i>",
        reply_markup=get_admin_resolve_options_keyboard(event_id, event.options),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_bet:pick_winner:"), IsAdminCallbackFilter())
async def cb_admin_pick_winner(callback: CallbackQuery, db_session: AsyncSession) -> None:
    parts = callback.data.split(":")
    event_id, option_id = int(parts[2]), int(parts[3])

    event = await bet_service.get_event_detail(db_session, event_id)
    if event is None:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return

    winning_option = next((o for o in event.options if o.id == option_id), None)
    if winning_option is None:
        await callback.answer("❌ Opzione non trovata.", show_alert=True)
        return

    preview = bet_service.compute_payout_preview(event, option_id)
    total_pot = preview["total_pot"]
    winning_count = preview["winning_count"]
    losing_count = preview["losing_count"]
    winning_pool = preview["winning_pool"]
    losing_pool = total_pot - winning_pool

    preview_lines = []
    for p in preview["previews"][:8]:
        preview_lines.append(
            f"  · bet {p['bet_amount']} 🪙 → payout <b>{p['payout']} 🪙</b>"
        )
    if len(preview["previews"]) > 8:
        preview_lines.append(f"  <i>... e altri {len(preview['previews']) - 8} vincitori</i>")

    preview_text = (
        "\n".join(preview_lines)
        if preview_lines
        else "  <i>Nessuna scommessa su questa opzione.</i>"
    )

    await callback.message.edit_text(
        f"📊 <b>Anteprima distribuzione</b>\n\n"
        f"🏆 Opzione vincente: <b>{esc(winning_option.label)}</b>\n"
        f"💰 Pool totale: <b>{total_pot} 🪙</b>\n\n"
        f"✅ Vincitori: {winning_count} scommesse ({winning_pool} 🪙 puntati)\n"
        f"❌ Perdenti: {losing_count} scommesse ({losing_pool} 🪙 persi)\n\n"
        f"<b>Payout proporzionale:</b>\n{preview_text}\n\n"
        f"<i>I {total_pot} 🪙 totali vengono ridistribuiti ai vincitori "
        f"in proporzione al loro importo scommesso.</i>\n\n"
        f"Confermi?",
        reply_markup=get_admin_confirm_resolve_keyboard(event_id, option_id),
    )
    await callback.answer()


@router.callback_query(
    F.data.startswith("admin_bet:confirm_resolve:"), IsAdminCallbackFilter()
)
async def cb_admin_confirm_resolve(
    callback: CallbackQuery, db_session: AsyncSession
) -> None:
    parts = callback.data.split(":")
    event_id, option_id = int(parts[2]), int(parts[3])

    try:
        summary = await bet_service.resolve_event(db_session, event_id, option_id)
        # Resolving straight from `open` (before any lock) leaves a pending auto-lock
        # task orphaned — cancel it so the scheduler doesn't later no-op on it.
        await bet_service.cancel_pending_close(db_session, event_id)
        await db_session.commit()
    except EventNotFoundError:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return
    except EventAlreadySettledError as e:
        await callback.answer(
            f"⚠️ Evento già chiuso (stato: {e.status}).", show_alert=True
        )
        return

    # Award winner milestones (best-effort) and announce them in the group.
    try:
        for w in summary["winners_data"]:
            milestones = await badge_service.check_and_award_milestones(
                db_session, w["tg_id"]
            )
            if milestones:
                await db_session.commit()
                await announce_trophies(callback.bot, db_session, w["tg_id"], milestones)
    except Exception:
        pass

    # Notify winners and losers in private (fire-and-forget)
    event = await bet_service.get_event_detail(db_session, event_id)
    if event:
        for bet in event.user_bets:
            if bet.status == "won":
                payout = next(
                    (w["payout"] for w in summary["winners_data"] if w["tg_id"] == bet.user_tg_id),
                    0,
                )
                try:
                    await callback.bot.send_message(
                        bet.user_tg_id,
                        f"🏆 <b>Hai vinto!</b>\n\n"
                        f"🎯 <b>{esc(event.title)}</b>\n"
                        f"✅ Opzione vincente: <b>{esc(summary['winning_option'])}</b>\n"
                        f"💸 Hai scommesso: <b>{bet.amount} 🪙</b>\n"
                        f"🎉 <b>Payout ricevuto: {payout} 🪙</b>",
                    )
                except Exception:
                    pass
            elif bet.status == "lost":
                try:
                    await callback.bot.send_message(
                        bet.user_tg_id,
                        f"❌ <b>Scommessa persa.</b>\n\n"
                        f"🎯 <b>{esc(event.title)}</b>\n"
                        f"✅ Opzione vincente: <b>{esc(summary['winning_option'])}</b>\n"
                        f"💸 Hai perso: <b>{bet.amount} 🪙</b>",
                    )
                except Exception:
                    pass

    await callback.message.edit_text(
        f"🏁 <b>Evento #{event_id} risolto!</b>\n\n"
        f"✅ Opzione vincente: <b>{esc(summary['winning_option'])}</b>\n"
        f"💰 Pool totale: <b>{summary['total_pot']} 🪙</b>\n"
        f"🏆 Vincitori: <b>{summary['winners']}</b>\n"
        f"💸 CoInn distribuiti: <b>{summary['total_distributed']} 🪙</b>\n\n"
        f"<i>I vincitori sono stati notificati in privato.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Torna alla lista", callback_data="admin_bet:list"),
            InlineKeyboardButton(text="✖ Chiudi", callback_data="admin_bet:close"),
        ]]),
    )
    await callback.answer("✅ Risolto!")


# --- CANCEL ---

@router.callback_query(F.data.startswith("admin_bet:cancel:"), IsAdminCallbackFilter())
async def cb_admin_cancel(callback: CallbackQuery, db_session: AsyncSession) -> None:
    event_id = int(callback.data.split(":")[2])
    event = await bet_service.get_event_detail(db_session, event_id)
    if event is None:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return
    if event.status in ("resolved", "cancelled"):
        await callback.answer("⚠️ Evento già chiuso.", show_alert=True)
        return

    pending_count = sum(1 for b in event.user_bets if b.status == "pending")
    total_to_refund = sum(b.amount for b in event.user_bets if b.status == "pending")

    await callback.message.edit_text(
        f"❌ <b>Annulla scommessa — #{event.id} {esc(event.title)}</b>\n\n"
        f"🎫 Scommesse da rimborsare: <b>{pending_count}</b>\n"
        f"💰 Totale rimborso: <b>{total_to_refund} 🪙</b>\n\n"
        f"⚠️ <b>Attenzione:</b> questa operazione è irreversibile. "
        f"Tutti i partecipanti riceveranno indietro l'importo scommesso.\n\n"
        f"Confermi l'annullamento?",
        reply_markup=get_admin_confirm_cancel_keyboard(event_id, pending_count),
    )
    await callback.answer()


@router.callback_query(
    F.data.startswith("admin_bet:confirm_cancel:"), IsAdminCallbackFilter()
)
async def cb_admin_confirm_cancel(
    callback: CallbackQuery, db_session: AsyncSession
) -> None:
    event_id = int(callback.data.split(":")[2])

    try:
        result = await bet_service.cancel_event(db_session, event_id)
        await bet_service.cancel_pending_close(db_session, event_id)
        await db_session.commit()
    except EventNotFoundError:
        await callback.answer("❌ Evento non trovato.", show_alert=True)
        return
    except EventAlreadySettledError as e:
        await callback.answer(
            f"⚠️ Evento già chiuso (stato: {e.status}).", show_alert=True
        )
        return

    # Notify each bettor of their refund (best-effort)
    for r in result["refunds_data"]:
        try:
            await callback.bot.send_message(
                r["tg_id"],
                f"↩️ <b>Scommessa rimborsata.</b>\n\n"
                f"🎯 <b>{esc(result['title'])}</b> è stata annullata dall'admin.\n"
                f"💰 <b>Rimborso: {r['amount']} 🪙</b> accreditati sul tuo saldo.",
            )
        except Exception:
            pass

    await callback.message.edit_text(
        f"↩️ <b>Evento #{event_id} annullato.</b>\n\n"
        f"💰 Rimborsi emessi: <b>{result['refunded']}</b>\n\n"
        f"<i>I partecipanti sono stati notificati in privato.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Torna alla lista", callback_data="admin_bet:list"),
            InlineKeyboardButton(text="✖ Chiudi", callback_data="admin_bet:close"),
        ]]),
    )
    await callback.answer("↩️ Annullato!")


# --- CLOSE ---

@router.callback_query(F.data == "admin_bet:close", IsAdminCallbackFilter())
async def cb_admin_close(callback: CallbackQuery) -> None:
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# --- Deny non-admins ---

@router.callback_query(F.data.startswith("admin_bet:"))
async def cb_admin_deny(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Accesso non autorizzato.", show_alert=True)
