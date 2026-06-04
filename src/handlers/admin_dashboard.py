"""
Button-driven admin dashboard (namespace ``adm:*``).

A full inline UI so admins can run everything without typing commands: stats,
leaderboard, audit, quiz launch/close/create, airdrop, and per-user actions
(credit/debit/set balance, ban/kick/sban, mute/unmute, warn/unwarn) via a
paginated user picker.

It does NOT reimplement business logic: every action goes through the same
service layer + audit log as the text commands (handlers/admin.py,
handlers/quiz.py, services/*), so the two paths behave identically. Mutating
callbacks are gated by IsAdminCallbackFilter, with a catch-all ``adm:`` deny at
the bottom — mirroring handlers/admin.py and handlers/admin_betting.py.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
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

from config_data.config import settings
from database.models import TransactionType, Wallet
from exceptions.economy import InsufficientFundsError, WalletNotFoundError
from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.admin import apply_warning, render_audit, render_panel_help, render_stats
from handlers.admin_betting import _show_event_list
from handlers.leaderboard import render_board
from handlers.quiz import close_quiz, open_quiz, start_quiz_creation
from keyboards.admin_dashboard_kb import (
    PAGE_SIZE,
    back_home_kb,
    cancel_to_home_kb,
    cancel_to_user_kb,
    confirm_kb,
    econ_kb,
    home_kb,
    lead_kb,
    quiz_hub_kb,
    skip_or_cancel_reason_kb,
    user_detail_kb,
    users_kb,
)
from services import admin_service, economy_service, moderation_service, quiz_service, xp_service
from services.xp_service import XpSource

log = logging.getLogger(__name__)
router = Router()

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


@router.callback_query(F.data == "adm:home", IsAdminCallbackFilter())
async def cb_home(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    await show_dashboard_home(callback, db_session, edit=True)


@router.callback_query(F.data == "adm:stats", IsAdminCallbackFilter())
async def cb_stats(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(await render_stats(db_session), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:lead", IsAdminCallbackFilter())
async def cb_lead(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(
        await render_board(db_session, "coins"), reply_markup=lead_kb("coins")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:lead:"), IsAdminCallbackFilter())
async def cb_lead_board(callback: CallbackQuery, db_session: AsyncSession) -> None:
    board = callback.data.split(":")[2]
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


@router.callback_query(F.data == "adm:audit", IsAdminCallbackFilter())
async def cb_audit(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await callback.message.edit_text(await render_audit(db_session), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:help", IsAdminCallbackFilter())
async def cb_help(callback: CallbackQuery) -> None:
    await callback.message.edit_text(render_panel_help(), reply_markup=back_home_kb())
    await callback.answer()


@router.callback_query(F.data == "adm:close", IsAdminCallbackFilter())
async def cb_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()


# ---------------------------------------------------------------------------
# Quiz hub
# ---------------------------------------------------------------------------

async def _render_quiz_hub(message: Message, db_session: AsyncSession) -> None:
    quizzes = await quiz_service.list_ready(db_session)  # ready + running
    if not quizzes:
        text = "🧠 <b>Quiz</b>\n\n<i>Nessun quiz pronto o in corso. Creane uno!</i>"
    else:
        lines = ["🧠 <b>Quiz</b>\n"]
        for q in quizzes:
            status = {"ready": "🟡 pronto", "running": "🟢 in corso"}.get(q.status, q.status)
            lines.append(
                f"#{q.id} <b>{q.title}</b> — {status} · {len(q.questions)} dom.\n"
                f"   💰 {quiz_service.format_prize_summary(q)}"
            )
        text = "\n".join(lines)
    try:
        await message.edit_text(text, reply_markup=quiz_hub_kb(quizzes))
    except Exception:  # noqa: BLE001
        await message.answer(text, reply_markup=quiz_hub_kb(quizzes))


@router.callback_query(F.data == "adm:quiz", IsAdminCallbackFilter())
async def cb_quiz_hub(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    await _render_quiz_hub(callback.message, db_session)
    await callback.answer()


@router.callback_query(F.data.startswith("adm:quiz:open:"), IsAdminCallbackFilter())
async def cb_quiz_open(callback: CallbackQuery, db_session: AsyncSession) -> None:
    quiz_id = int(callback.data.split(":")[3])
    ok, msg = await open_quiz(callback.bot, db_session, quiz_id)
    if ok:
        await db_session.commit()
    await callback.answer(msg, show_alert=not ok)
    await _render_quiz_hub(callback.message, db_session)


@router.callback_query(F.data.startswith("adm:quiz:close:"), IsAdminCallbackFilter())
async def cb_quiz_close(callback: CallbackQuery, db_session: AsyncSession) -> None:
    quiz_id = int(callback.data.split(":")[3])
    ok, msg = await close_quiz(callback.bot, db_session, quiz_id)
    await callback.answer("🏁 Quiz chiuso. Podio pubblicato." if ok else msg, show_alert=not ok)
    await _render_quiz_hub(callback.message, db_session)


@router.callback_query(F.data == "adm:quiz:new", IsAdminCallbackFilter())
async def cb_quiz_new(callback: CallbackQuery, state: FSMContext) -> None:
    await start_quiz_creation(callback.message, state, creator_id=callback.from_user.id)
    await callback.answer()


# ---------------------------------------------------------------------------
# Bets (reuse the existing betting management UI)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:bets", IsAdminCallbackFilter())
async def cb_bets(callback: CallbackQuery, db_session: AsyncSession) -> None:
    await _show_event_list(callback, db_session, edit=True)


# ---------------------------------------------------------------------------
# Economy (airdrop)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm:econ", IsAdminCallbackFilter())
async def cb_econ(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        "💰 <b>Economia</b>\n\nGestisci la valuta della community:", reply_markup=econ_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "adm:airdrop", IsAdminCallbackFilter())
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


@router.callback_query(F.data == "adm:xpairdrop", IsAdminCallbackFilter())
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

@router.callback_query(F.data.startswith("adm:users:"), IsAdminCallbackFilter())
async def cb_users(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    page = max(0, int(callback.data.split(":")[2]))
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


@router.callback_query(F.data == "adm:search", IsAdminCallbackFilter())
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
        await message.answer(f"🔍 Nessun utente trovato per «{query}».", reply_markup=back_home_kb())
        return
    ids = [u.tg_id for u in users]
    coins = dict(
        (await db_session.execute(select(Wallet.tg_id, Wallet.coins).where(Wallet.tg_id.in_(ids)))).all()
    )
    rows = [(u, coins.get(u.tg_id, 0)) for u in users]
    await message.answer(
        f"🔍 <b>Risultati per «{query}»: {len(rows)}</b>",
        reply_markup=users_kb(rows, page=0, has_next=False),
    )


@router.callback_query(F.data.startswith("adm:user:"), IsAdminCallbackFilter())
async def cb_user(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    tg_id = int(callback.data.split(":")[2])
    await _show_detail_cb(callback, db_session, tg_id)
    await callback.answer()


# ---------------------------------------------------------------------------
# Per-user actions
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("adm:act:"), IsAdminCallbackFilter())
async def cb_act(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, action, raw = callback.data.split(":")
    tg_id = int(raw)
    if action in ("mute", "warn") and settings.group_id == 0:
        await callback.answer("⚠️ GROUP_ID non configurato.", show_alert=True)
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


@router.callback_query(F.data.startswith("adm:ask:"), IsAdminCallbackFilter())
async def cb_ask(callback: CallbackQuery) -> None:
    _, _, action, raw = callback.data.split(":")
    tg_id = int(raw)
    guard = _mod_guard(callback.from_user.id, tg_id)
    if guard:
        await callback.answer(guard, show_alert=True)
        return
    label = {"ban": "BAN", "kick": "KICK"}.get(action, action.upper())
    await callback.message.edit_text(
        f"⚠️ Confermi <b>{label}</b> per <code>{tg_id}</code>?",
        reply_markup=confirm_kb(action, tg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:do:"), IsAdminCallbackFilter())
async def cb_do(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession) -> None:
    await state.clear()
    _, _, action, raw = callback.data.split(":")
    tg_id = int(raw)
    bot, admin_id, chat_id = callback.bot, callback.from_user.id, settings.group_id

    if action == "warn":  # warn with no reason (from the «Senza motivo» button)
        if chat_id == 0:
            await callback.answer("⚠️ GROUP_ID non configurato.", show_alert=True)
            return
        count, _esc = await apply_warning(bot, db_session, admin_id, tg_id, chat_id, None)
        await db_session.commit()
        await callback.answer(f"⚠️ Warn registrato (#{count}).")
        await _show_detail_cb(callback, db_session, tg_id)
        return

    if action != "unwarn" and chat_id == 0:
        await callback.answer("⚠️ GROUP_ID non configurato.", show_alert=True)
        return
    if action in ("ban", "kick") and tg_id == admin_id:
        await callback.answer("⚠️ Non puoi moderare te stesso.", show_alert=True)
        return

    success = True
    if action == "ban":
        success, err = await moderation_service.ban(bot, chat_id, tg_id)
        toast = "⛔ Utente bannato." if success else f"❌ {err}"
        if success:
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
        await _show_detail_msg(message, db_session, tg_id, prefix=toast)
        return

    try:
        if action == "credit":
            await economy_service.credit(
                db_session, tg_id, amount, TransactionType.admin_credit, f"Credito dashboard da #{admin_id}"
            )
            await admin_service.log_action(db_session, admin_id, "credita", target_tg_id=tg_id, amount=amount)
            toast = f"✅ Accreditati {amount:,} 🪙."
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
    token = (message.text or "").strip()
    if not moderation_service.looks_like_duration(token):
        await message.answer(
            "⚠️ Durata non valida. Esempi: <code>10m</code>, <code>1h</code>, <code>2d</code>.",
            reply_markup=cancel_to_user_kb(tg_id),
        )
        return
    duration = moderation_service.parse_duration(token)
    chat_id = settings.group_id
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
    reason = (message.text or "").strip()[:256]
    chat_id = settings.group_id
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

def _parse_amount(text: str | None) -> int | None:
    try:
        return int((text or "").strip())
    except ValueError:
        return None


def _mod_guard(admin_id: int, tg_id: int) -> str | None:
    if settings.group_id == 0:
        return "⚠️ GROUP_ID non configurato."
    if tg_id == admin_id:
        return "⚠️ Non puoi moderare te stesso."
    return None


async def render_user_detail(bot, db_session: AsyncSession, tg_id: int):
    """Returns (text, keyboard) for a user dossier, or None if unknown."""
    dossier = await admin_service.get_dossier(db_session, tg_id)
    if dossier is None:
        return None
    u = dossier.user
    username = f"@{u.username}" if u.username else "N/D"
    status_line = ""
    if settings.group_id != 0:
        try:
            member = await bot.get_chat_member(settings.group_id, tg_id)
            status_line = f"\n👥 Stato gruppo: {_STATUS_MAP.get(member.status, member.status)}"
        except Exception:  # noqa: BLE001
            pass
    rank = xp_service.rank_for_xp(u.xp)
    rank_str = f"{rank.emoji} {rank.name}" if rank else "—"
    tag_line = f"\n🏷️ Tag: {u.cosmetic_tag}" if u.cosmetic_tag else ""
    text = (
        f"🪪 <b>{u.full_name}</b>\n\n"
        f"🔖 {username}\n"
        f"🆔 <code>{u.tg_id}</code>\n"
        f"💰 Saldo: <b>{dossier.coins:,} 🪙</b>\n"
        f"⚡ XP: {u.xp:,} · 🎖️ Rango: {rank_str}{tag_line}\n"
        f"🏆 Trofei: {dossier.badge_count}\n"
        f"🎲 Scommesse: {dossier.bet_count} (vinte: {u.bets_won})\n"
        f"⚠️ Warn attivi: <b>{dossier.active_warnings}</b>"
        f"{status_line}\n\n"
        "Scegli un'azione:"
    )
    return text, user_detail_kb(tg_id, settings.group_id != 0)


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
@router.callback_query(F.data.startswith("adm:"))
async def cb_deny(callback: CallbackQuery) -> None:
    await callback.answer("⛔ Accesso non autorizzato.", show_alert=True)
