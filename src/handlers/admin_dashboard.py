"""
Button-driven admin dashboard (namespace ``adm:*``).

A full inline UI so admins can run everything without typing commands: stats,
leaderboard, audit, airdrop, and per-user actions (credit/debit/set balance,
ban/kick/sban, mute/unmute, warn/unwarn) via a paginated user picker. Events
(quiz · sondaggi · scommesse) live in their own hub (handlers/events.py, ``ev:*``),
reached from the "🎬 Eventi" button.

It does NOT reimplement business logic: every action goes through the same
service layer + audit log as the text commands (handlers/admin.py, services/*),
so the two paths behave identically. Mutating
callbacks are gated by IsAdminCallbackFilter, with a catch-all ``adm:`` deny at
the bottom — mirroring handlers/admin.py and handlers/admin_betting.py.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.filters.command import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import TransactionType, Wallet
from exceptions.economy import InsufficientFundsError, WalletNotFoundError
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.admin import apply_warning, render_audit, render_panel_help, render_stats
from handlers.admin_betting import _show_event_list
from handlers.callbacks import AdminCb
from handlers.leaderboard import render_board
from middlewares import ban_guard
from keyboards.admin_dashboard_kb import (
    PAGE_SIZE,
    back_home_kb,
    cancel_to_home_kb,
    cancel_to_user_kb,
    confirm_kb,
    econ_kb,
    home_kb,
    lead_kb,
    skip_or_cancel_reason_kb,
    user_detail_kb,
    users_kb,
)
from services import (
    admin_service,
    economy_service,
    group_registry,
    moderation_service,
    xp_service,
)
from services.xp_service import XpSource
from utils.text import esc

log = logging.getLogger(__name__)
router = Router()
# 100%-admin router: gate every message/callback at the root (STEERING §8). The
# per-handler filters + `adm:` deny catch-all stay as defense in depth.
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminCallbackFilter())

_MAX_AMOUNT = 10_000_000
_STATUS_MAP = {
    "creator": "👑 Creatore", "administrator": "🛡️ Admin", "member": "✅ Membro",
    "restricted": "🔇 Limitato", "left": "🚪 Uscito", "kicked": "⛔ Bannato",
}


class AdminPanelStates(StatesGroup):
    waiting_amount = State()      # credit / debit / setbal / xpgrant / xpset
    waiting_duration = State()    # mute
    waiting_reason = State()      # warn
    waiting_search = State()      # user search
    waiting_airdrop = State()     # mass credit
    waiting_xp_airdrop = State()  # mass XP grant


# ---------------------------------------------------------------------------
# Home + read-only sections
# ---------------------------------------------------------------------------

async def show_dashboard_home(
    target: Message | CallbackQuery, db_session: AsyncSession, edit: bool = False
) -> None:
    text = "⚙️ <b>Dashboard Admin</b>\n\n" + await render_stats(db_session) + "\n\nScegli una sezione:"
    if isinstance(target, CallbackQuery):
        try:
            if edit:
                await target.message.edit_text(text, reply_markup=home_kb())
            else:
                await target.message.answer(text, reply_markup=home_kb())
        except Exception:  # noqa: BLE001 — "message not modified" etc.
            await target.message.answer(text, reply_markup=home_kb())
        await target.answer()
    else:
        await target.answer(text, reply_markup=home_kb())


@router.message(Command("admin"), IsAdminFilter())
async def cmd_admin(message: Message, db_session: AsyncSession) -> None:
    if message.chat.type != ChatType.PRIVATE:
        bot_info = await message.bot.get_me()
        await message.reply(
            "⚙️ Apri la dashboard admin in chat privata:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="⚙️ Apri dashboard",
                    url=f"https://t.me/{bot_info.username}?start=admin",
                )
            ]]),
        )
        return
    await show_dashboard_home(message, db_session)


@router.callback_query(AdminCb.filter(F.action == "home"), IsAdminCallbackFilter())
async def cb_home(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    await show_dashboard_home(callback, db_session, edit=True)


@router.callback_query(AdminCb.filter(F.action == "stats"), IsAdminCallbackFilter())
async def cb_stats(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(await render_stats(db_session), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "lead"), IsAdminCallbackFilter())
async def cb_lead(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(
        await render_board(db_session, "coins"), reply_markup=lead_kb("coins")
    )
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "lead_board"), IsAdminCallbackFilter())
async def cb_lead_board(
    callback: CallbackQuery, callback_data: AdminCb, db_session: AsyncSession
) -> None:
    board = callback_data.key
    if board not in ("coins", "xp", "trofei"):
        await callback.answer()
        return
    try:
        await callback.message.edit_text(
            await render_board(db_session, board), reply_markup=lead_kb(board)
        )
    except Exception:  # noqa: BLE001 — "message not modified"
        pass
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "audit"), IsAdminCallbackFilter())
async def cb_audit(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(await render_audit(db_session), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "help"), IsAdminCallbackFilter())
async def cb_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(render_panel_help(), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "close"), IsAdminCallbackFilter())
async def cb_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()


# ---------------------------------------------------------------------------
# Bets (reuse the existing betting management UI)
# ---------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "bets"), IsAdminCallbackFilter())
async def cb_bets(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_event_list(callback, db_session)


# ---------------------------------------------------------------------------
# Economy (airdrop)
# ---------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "econ"), IsAdminCallbackFilter())
async def cb_econ(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "💰 <b>Economia</b>\n\nGestisci la valuta della community:", reply_markup=econ_kb()
    )
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "airdrop"), IsAdminCallbackFilter())
async def cb_airdrop(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanelStates.waiting_airdrop)
    await callback.message.edit_text(
        "🎁 <b>Airdrop</b>\n\nInvia l'importo da accreditare a <b>tutti</b> gli utenti:",
        reply_markup=cancel_to_home_kb(),
    )
    await callback.answer()


@router.message(AdminPanelStates.waiting_airdrop, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_airdrop(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    amount = _parse_amount(message.text)
    if amount is None or amount <= 0 or amount > _MAX_AMOUNT:
        await message.answer(f"⚠️ Importo non valido (1–{_MAX_AMOUNT:,}).", reply_markup=cancel_to_home_kb())
        return
    count = await admin_service.mass_credit(db_session, amount, "Airdrop da dashboard")
    await admin_service.log_action(
        db_session, message.from_user.id, "airdrop", amount=amount, detail=f"{count} utenti"
    )
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"🎁 <b>Airdrop!</b> Accreditati <b>{amount:,} 🪙</b> a <b>{count}</b> utenti.",
        reply_markup=back_home_kb(),
    )


@router.callback_query(AdminCb.filter(F.action == "xpairdrop"), IsAdminCallbackFilter())
async def cb_xpairdrop(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanelStates.waiting_xp_airdrop)
    await callback.message.edit_text(
        "⚡ <b>Airdrop XP</b>\n\nInvia gli XP da assegnare a <b>tutti</b> gli utenti:",
        reply_markup=cancel_to_home_kb(),
    )
    await callback.answer()


@router.message(AdminPanelStates.waiting_xp_airdrop, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_xp_airdrop(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    amount = _parse_amount(message.text)
    if amount is None or amount <= 0 or amount > _MAX_AMOUNT:
        await message.answer(f"⚠️ Valore non valido (1–{_MAX_AMOUNT:,}).", reply_markup=cancel_to_home_kb())
        return
    count = await xp_service.airdrop_xp(db_session, amount)
    await admin_service.log_action(
        db_session, message.from_user.id, "xp_airdrop", amount=amount, detail=f"{count} utenti"
    )
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"⚡ <b>Airdrop XP!</b> Assegnati <b>+{amount:,} XP</b> a <b>{count}</b> utenti.",
        reply_markup=back_home_kb(),
    )


# ---------------------------------------------------------------------------
# User picker + search
# ---------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "users"), IsAdminCallbackFilter())
async def cb_users(
    callback: CallbackQuery, callback_data: AdminCb, state: FSMContext, db_session: AsyncSession
) -> None:
    await state.clear()
    page = callback_data.item_id
    if page is None:
        await callback.answer()
        return
    page = max(0, page)
    rows = await admin_service.list_users(db_session, page * PAGE_SIZE, PAGE_SIZE + 1)
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    if not rows and page == 0:
        await callback.message.edit_text("👥 <i>Nessun utente registrato.</i>", reply_markup=back_home_kb())
        await callback.answer()
        return
    await callback.message.edit_text(
        f"👥 <b>Utenti</b> — pagina {page + 1}\nScegli un utente per gestirlo:",
        reply_markup=users_kb(rows, page, has_next),
    )
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "search"), IsAdminCallbackFilter())
async def cb_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminPanelStates.waiting_search)
    await callback.message.edit_text(
        "🔍 Invia un nome o <code>@username</code> da cercare:", reply_markup=cancel_to_home_kb()
    )
    await callback.answer()


@router.message(AdminPanelStates.waiting_search, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_search(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    query = (message.text or "").strip()
    await state.clear()
    if len(query) < 2:
        await message.answer("⚠️ Inserisci almeno 2 caratteri.", reply_markup=back_home_kb())
        return
    users = await admin_service.search_users(db_session, query)
    if not users:
        await message.answer(f"🔍 Nessun utente trovato per «{esc(query)}».", reply_markup=back_home_kb())
        return
    ids = [u.tg_id for u in users]
    coins = dict(
        (await db_session.execute(select(Wallet.tg_id, Wallet.coins).where(Wallet.tg_id.in_(ids)))).all()
    )
    rows = [(u, coins.get(u.tg_id, 0)) for u in users]
    await message.answer(
        f"🔍 <b>Risultati per «{esc(query)}»: {len(rows)}</b>",
        reply_markup=users_kb(rows, page=0, has_next=False),
    )


@router.callback_query(AdminCb.filter(F.action == "user"), IsAdminCallbackFilter())
async def cb_user(
    callback: CallbackQuery, callback_data: AdminCb, state: FSMContext, db_session: AsyncSession
) -> None:
    await state.clear()
    tg_id = callback_data.item_id
    if tg_id is None:
        await callback.answer()
        return
    await _show_detail_cb(callback, db_session, tg_id)
    await callback.answer()


# ---------------------------------------------------------------------------
# Per-user actions
# ---------------------------------------------------------------------------

@router.callback_query(AdminCb.filter(F.action == "act"), IsAdminCallbackFilter())
async def cb_act(callback: CallbackQuery, callback_data: AdminCb, state: FSMContext) -> None:
    action = callback_data.key
    tg_id = callback_data.item_id
    if action is None or tg_id is None:
        await callback.answer()
        return
    if action in ("mute", "warn"):
        guard = _mod_guard(callback.from_user.id, tg_id, callback.bot.id)
        if guard:
            await callback.answer(guard, show_alert=True)
            return
    await state.update_data(action=action, target_tg_id=tg_id)
    if action in ("credit", "debit", "setbal"):
        prompts = {
            "credit": "da <b>accreditare</b>", "debit": "da <b>addebitare</b>",
            "setbal": "come <b>nuovo saldo</b>",
        }
        await state.set_state(AdminPanelStates.waiting_amount)
        await callback.message.edit_text(
            f"💰 Invia l'importo {prompts[action]} per <code>{tg_id}</code>:",
            reply_markup=cancel_to_user_kb(tg_id),
        )
    elif action in ("xpgrant", "xpset"):
        prompt = "da <b>assegnare</b>" if action == "xpgrant" else "come <b>nuovo totale</b>"
        await state.set_state(AdminPanelStates.waiting_amount)
        await callback.message.edit_text(
            f"⚡ Invia gli <b>XP</b> {prompt} per <code>{tg_id}</code>:",
            reply_markup=cancel_to_user_kb(tg_id),
        )
    elif action == "mute":
        await state.set_state(AdminPanelStates.waiting_duration)
        await callback.message.edit_text(
            f"🔇 Invia la <b>durata</b> del mute per <code>{tg_id}</code> (es. 10m, 1h, 2d):",
            reply_markup=cancel_to_user_kb(tg_id),
        )
    elif action == "warn":
        await state.set_state(AdminPanelStates.waiting_reason)
        await callback.message.edit_text(
            f"⚠️ Invia il <b>motivo</b> del warn per <code>{tg_id}</code> (oppure senza motivo):",
            reply_markup=skip_or_cancel_reason_kb(tg_id),
        )
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "ask"), IsAdminCallbackFilter())
async def cb_ask(callback: CallbackQuery, callback_data: AdminCb) -> None:
    action = callback_data.key
    tg_id = callback_data.item_id
    if action is None or tg_id is None:
        await callback.answer()
        return
    guard = _mod_guard(callback.from_user.id, tg_id, callback.bot.id)
    if guard:
        await callback.answer(guard, show_alert=True)
        return
    label = {"ban": "BAN", "kick": "KICK"}.get(action, action.upper())
    await callback.message.edit_text(
        f"⚠️ Confermi <b>{label}</b> per <code>{tg_id}</code>?",
        reply_markup=confirm_kb(action, tg_id),
    )
    await callback.answer()


@router.callback_query(AdminCb.filter(F.action == "do"), IsAdminCallbackFilter())
async def cb_do(
    callback: CallbackQuery, callback_data: AdminCb, state: FSMContext, db_session: AsyncSession
) -> None:
    await state.clear()
    action = callback_data.key
    tg_id = callback_data.item_id
    if action is None or tg_id is None:
        await callback.answer()
        return
    bot, admin_id, chat_id = callback.bot, callback.from_user.id, group_registry.get_group_id()

    if action == "warn":  # warn with no reason (from the «Senza motivo» button)
        guard = _mod_guard(admin_id, tg_id, bot.id)
        if guard:
            await callback.answer(guard, show_alert=True)
            return
        count, _esc = await apply_warning(bot, db_session, admin_id, tg_id, chat_id, None)
        await db_session.commit()
        ban_guard.invalidate(tg_id)  # a warn may have auto-banned the user
        await callback.answer(f"⚠️ Warn registrato (#{count}).")
        await _show_detail_cb(callback, db_session, tg_id)
        return

    if action != "unwarn":
        guard = _mod_guard(admin_id, tg_id, bot.id)
        if guard:
            await callback.answer(guard, show_alert=True)
            return

    success = True
    if action == "ban":
        # Bot-level ban applies on the admin's intent regardless of the group-removal
        # result, so a removed user can't keep using the bot in private. /sban reverses it.
        # `success` stays False on a failed group-removal only to surface the warning
        # toast (show_alert) — the bot-ban itself always lands.
        success, err = await moderation_service.ban(bot, chat_id, tg_id)
        toast = "⛔ Utente bannato (anche in privato)." if success else f"⛔ Bannato dal bot.\n⚠️ Rimozione dal gruppo non riuscita: {err}"
        await admin_service.set_user_banned(db_session, tg_id, True)
        await admin_service.log_action(db_session, admin_id, "ban", target_tg_id=tg_id, group_id=chat_id)
    elif action == "kick":
        success, err = await moderation_service.kick(bot, chat_id, tg_id)
        toast = "👢 Utente espulso." if success else f"❌ {err}"
        if success:
            await admin_service.log_action(db_session, admin_id, "kick", target_tg_id=tg_id, group_id=chat_id)
    elif action == "sban":
        success, err = await moderation_service.unban(bot, chat_id, tg_id)
        toast = "✅ Utente sbannato." if success else f"❌ {err}"
        if success:
            await admin_service.set_user_banned(db_session, tg_id, False)
            await admin_service.log_action(db_session, admin_id, "sban", target_tg_id=tg_id, group_id=chat_id)
    elif action == "unmute":
        success, err = await moderation_service.unmute(bot, chat_id, tg_id)
        toast = "🔊 Utente riabilitato." if success else f"❌ {err}"
        if success:
            await admin_service.log_action(db_session, admin_id, "unmute", target_tg_id=tg_id, group_id=chat_id)
    elif action == "unwarn":
        cleared = await admin_service.clear_warnings(db_session, tg_id, count=1)
        if cleared:
            remaining = await admin_service.active_warning_count(db_session, tg_id)
            await admin_service.log_action(db_session, admin_id, "unwarn", target_tg_id=tg_id, amount=remaining)
            toast = f"♻️ Warn rimosso (restano {remaining})."
        else:
            success, toast = False, "ℹ️ Nessun warn attivo."
    else:
        await callback.answer()
        return

    await db_session.commit()
    if action in ("ban", "sban"):
        ban_guard.invalidate(tg_id)  # apply the new bot-ban state on the next update
    await callback.answer(toast, show_alert=not success)
    await _show_detail_cb(callback, db_session, tg_id)


# ---------------------------------------------------------------------------
# FSM inputs (amount / duration / reason)
# ---------------------------------------------------------------------------

@router.message(AdminPanelStates.waiting_amount, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_amount(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    action, tg_id = data["action"], data["target_tg_id"]
    amount = _parse_amount(message.text)
    floor = 0 if action in ("setbal", "xpset") else 1
    if amount is None or amount < floor or amount > _MAX_AMOUNT:
        await message.answer(
            f"⚠️ Valore non valido ({floor}–{_MAX_AMOUNT:,}).", reply_markup=cancel_to_user_kb(tg_id)
        )
        return
    admin_id = message.from_user.id

    # XP actions (no coins) — dedicated path through xp_service + audit.
    if action in ("xpgrant", "xpset"):
        if action == "xpgrant":
            res = await xp_service.grant_xp(db_session, tg_id, amount, XpSource.admin_grant, capped=False)
            await admin_service.log_action(db_session, admin_id, "xp_grant", target_tg_id=tg_id, amount=amount)
            toast = f"⚡ +{res.granted:,} XP assegnati."
        else:  # xpset
            new_xp = await xp_service.set_xp(db_session, tg_id, amount)
            await admin_service.log_action(db_session, admin_id, "xp_set", target_tg_id=tg_id, amount=new_xp)
            toast = f"⚡ XP impostati a {new_xp:,}."
        await db_session.commit()
        await state.clear()
        if action == "xpgrant":
            dm = f"⚡ Hai ricevuto <b>+{res.granted:,} XP</b> da un amministratore!"
            if res.leveled_up:
                dm += f"\n🎉 Sei salito al <b>livello {res.new_level}</b>!"
            if res.new_rank:
                dm += f"\n🎖️ Nuovo rango: <b>{esc(res.new_rank.emoji)} {esc(res.new_rank.name)}</b>!"
            await _notify_dm(message.bot, tg_id, dm)
        await _show_detail_msg(message, db_session, tg_id, prefix=toast)
        return

    try:
        if action == "credit":
            await economy_service.credit(
                db_session, tg_id, amount, TransactionType.admin_credit, f"Credito dashboard da #{admin_id}"
            )
            await admin_service.log_action(db_session, admin_id, "credita", target_tg_id=tg_id, amount=amount)
            toast = f"✅ Accreditati {amount:,} 🪙."
            await db_session.commit()
            await state.clear()
            await _notify_dm(message.bot, tg_id, f"💰 Hai ricevuto <b>{amount:,} CoInn</b> da un amministratore! 🪙")
            await _show_detail_msg(message, db_session, tg_id, prefix=toast)
            return
        elif action == "debit":
            await economy_service.debit(
                db_session, tg_id, amount, TransactionType.admin_debit, f"Addebito dashboard da #{admin_id}"
            )
            await admin_service.log_action(db_session, admin_id, "addebita", target_tg_id=tg_id, amount=-amount)
            toast = f"🔻 Addebitati {amount:,} 🪙."
        else:  # setbal
            old, new = await admin_service.set_balance(db_session, tg_id, amount)
            await admin_service.log_action(
                db_session, admin_id, "setsaldo", target_tg_id=tg_id, amount=new, detail=f"{old} → {new}"
            )
            toast = f"⚖️ Saldo: {old:,} → {new:,} 🪙."
    except InsufficientFundsError as e:
        await message.answer(
            f"⚠️ Fondi insufficienti: ha {e.balance:,} 🪙 (richiesti {e.required:,}).",
            reply_markup=cancel_to_user_kb(tg_id),
        )
        return
    except (WalletNotFoundError, ValueError) as e:
        await message.answer(f"⚠️ {e}", reply_markup=cancel_to_user_kb(tg_id))
        return

    await db_session.commit()
    await state.clear()
    await _show_detail_msg(message, db_session, tg_id, prefix=toast)


@router.message(AdminPanelStates.waiting_duration, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_duration(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    tg_id = data["target_tg_id"]
    guard = _mod_guard(message.from_user.id, tg_id, message.bot.id)
    if guard:
        await state.clear()
        await _show_detail_msg(message, db_session, tg_id, prefix=guard)
        return
    token = (message.text or "").strip()
    if not moderation_service.looks_like_duration(token):
        await message.answer(
            "⚠️ Durata non valida. Esempi: <code>10m</code>, <code>1h</code>, <code>2d</code>.",
            reply_markup=cancel_to_user_kb(tg_id),
        )
        return
    duration = moderation_service.parse_duration(token)
    chat_id = group_registry.get_group_id()
    ok, err = await moderation_service.mute(message.bot, chat_id, tg_id, duration)
    if ok:
        await admin_service.log_action(
            db_session, message.from_user.id, "mute",
            target_tg_id=tg_id, group_id=chat_id, amount=duration,
        )
        await db_session.commit()
        toast = f"🔇 Silenziato per {duration // 60} min."
    else:
        toast = f"❌ {err}"
    await state.clear()
    await _show_detail_msg(message, db_session, tg_id, prefix=toast)


@router.message(AdminPanelStates.waiting_reason, IsAdminFilter(), ~F.text.startswith("/"))
async def fsm_reason(message: Message, state: FSMContext, db_session: AsyncSession) -> None:
    data = await state.get_data()
    tg_id = data["target_tg_id"]
    guard = _mod_guard(message.from_user.id, tg_id, message.bot.id)
    if guard:
        await state.clear()
        await _show_detail_msg(message, db_session, tg_id, prefix=guard)
        return
    reason = (message.text or "").strip()[:256]
    chat_id = group_registry.get_group_id()
    count, escalation = await apply_warning(
        message.bot, db_session, message.from_user.id, tg_id, chat_id, reason or None
    )
    await db_session.commit()
    await state.clear()
    toast = f"⚠️ Warn registrato (#{count})." + (escalation.replace("\n", " ") if escalation else "")
    await _show_detail_msg(message, db_session, tg_id, prefix=toast)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _notify_dm(bot, tg_id: int, text: str) -> None:
    """Best-effort DM to a user; silently ignored if the user never started the bot."""
    try:
        await bot.send_message(tg_id, text, parse_mode=ParseMode.HTML)
    except Exception:  # noqa: BLE001
        log.debug("DM notification to %s skipped (user may not have started the bot)", tg_id)


def _parse_amount(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _mod_guard(admin_id: int, tg_id: int, bot_id: int | None = None) -> str | None:
    if group_registry.get_group_id() == 0:
        return "⚠️ GROUP_ID non configurato."
    if tg_id == admin_id:
        return "⚠️ Non puoi moderare te stesso."
    if bot_id is not None and tg_id == bot_id:
        return "⚠️ Non posso moderare me stesso. 🤖"
    return None


async def render_user_detail(bot, db_session: AsyncSession, tg_id: int):
    """Returns (text, keyboard) for a user dossier, or None if unknown."""
    dossier = await admin_service.get_dossier(db_session, tg_id)
    if dossier is None:
        return None
    u = dossier.user
    username = f"@{esc(u.username)}" if u.username else "N/D"
    status_line = ""
    group_id = group_registry.get_group_id()
    if group_id != 0:
        try:
            member = await bot.get_chat_member(group_id, tg_id)
            status_line = f"\n👥 Stato gruppo: {_STATUS_MAP.get(member.status, member.status)}"
        except Exception:  # noqa: BLE001
            pass
    level = xp_service.level_for_xp(u.xp).level
    rank = xp_service.rank_for_level(level)
    rank_str = f"{rank.emoji} {esc(rank.name)}" if rank else "—"
    from services.shop_service import render_active_tags
    _tags = render_active_tags(u)
    tag_line = f"\n🏷️ Tag: {esc(_tags)}" if _tags else ""
    text = (
        f"🪪 <b>{esc(u.full_name)}</b>\n\n"
        f"🔖 {username}\n"
        f"🆔 <code>{u.tg_id}</code>\n"
        f"💰 Saldo: <b>{dossier.coins:,} 🪙</b>\n"
        f"⚡ Livello {level} · XP: {u.xp:,} · 🎖️ Rango: {rank_str}{tag_line}\n"
        f"🏆 Trofei: {dossier.badge_count}\n"
        f"🎲 Scommesse: {dossier.bet_count} (vinte: {u.bets_won})\n"
        f"⚠️ Warn attivi: <b>{dossier.active_warnings}</b>"
        f"{status_line}\n\n"
        "Scegli un'azione:"
    )
    return text, user_detail_kb(tg_id, group_id != 0)


async def _show_detail_cb(callback: CallbackQuery, db_session: AsyncSession, tg_id: int) -> None:
    rendered = await render_user_detail(callback.bot, db_session, tg_id)
    if rendered is None:
        await callback.message.edit_text("⚠️ Utente non trovato.", reply_markup=back_home_kb())
        return
    text, kb = rendered
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001
        await callback.message.answer(text, reply_markup=kb)


async def _show_detail_msg(
    message: Message, db_session: AsyncSession, tg_id: int, prefix: str = ""
) -> None:
    rendered = await render_user_detail(message.bot, db_session, tg_id)
    if rendered is None:
        await message.answer("⚠️ Utente non trovato.", reply_markup=back_home_kb())
        return
    text, kb = rendered
    await message.answer((f"{prefix}\n\n" if prefix else "") + text, reply_markup=kb)


# Catch-all: anything `adm:*` that reached here failed the admin filter above.
@router.callback_query(F.data.startswith(f"{AdminCb.__prefix__}:"))
async def cb_deny(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Accesso non autorizzato.", show_alert=True)
