"""Telegram adapter for the asynchronous three-phase narrative raid."""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from filters.admin_filter import IsAdminCallbackFilter, IsAdminFilter
from handlers.callbacks import EventCb, RaidCb, RaidCreateCb
from services import group_registry, raid_service, schedule_service
from services.raid_service import RaidSnapshot
from services.structured_ai import GeminiStructuredProvider
from utils.text import esc

log = logging.getLogger(__name__)
router = Router(name="raid")


class RaidCreateStates(StatesGroup):
    theme = State()
    building = State()


@router.message(RaidCreateStates.building, IsAdminFilter(), F.text)
async def creation_in_progress(message: Message) -> None:
    await message.answer("🐲 Sto già preparando questo raid. Attendi un momento o annulla.")


async def start_creation(message: Message, state: FSMContext, creator_id: int) -> None:
    await state.clear()
    await state.set_state(RaidCreateStates.theme)
    await message.answer(
        "🐉 <b>Nuovo raid narrativo</b>\n\n"
        "Scrivi un tema o una breve premessa (massimo 300 caratteri). "
        "Alduino preparerà boss, indizi e tre fasi; le regole resteranno locali e verificabili.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="❌ Annulla", callback_data=RaidCreateCb(action="cancel").pack(),
        )]]),
    )


@router.callback_query(RaidCreateCb.filter(F.action == "cancel"), IsAdminCallbackFilter())
async def cancel_creation(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    if current not in {
        RaidCreateStates.theme.state,
        RaidCreateStates.building.state,
    }:
        await callback.answer("Questa creazione non è più attiva.")
        return
    await state.clear()
    await callback.message.edit_text("❌ Creazione del raid annullata.")
    await callback.answer()


@router.message(
    RaidCreateStates.theme, IsAdminFilter(), F.text, ~F.text.startswith("/"),
)
async def create_from_theme(
    message: Message, state: FSMContext, db_session: AsyncSession,
) -> None:
    theme = " ".join(message.text.strip().split())
    if not 3 <= len(theme) <= 300:
        await message.answer(
            f"⚠️ Serve un tema da 3 a 300 caratteri (ora: {len(theme)})."
        )
        return
    # Consume the input state before the external call: two messages delivered
    # close together must not create two raids while Gemini is still working.
    await state.set_state(RaidCreateStates.building)
    wait_message = await message.answer("🐲 Alduino sta preparando il campo di battaglia…")
    blueprint, fallback = await raid_service.build_blueprint(
        theme, GeminiStructuredProvider(),
    )
    # A cancel callback can be processed while the provider is in flight.
    if await state.get_state() != RaidCreateStates.building.state:
        try:
            await wait_message.edit_text("❌ Creazione del raid annullata.")
        except Exception:  # noqa: BLE001 — the cancel was already confirmed elsewhere
            pass
        return
    root = await raid_service.create_raid(
        db_session, creator_tg_id=message.from_user.id, blueprint=blueprint,
    )
    await db_session.commit()
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(
        text="▶️ Avvia ora nel gruppo",
        callback_data=EventCb(
            action="askstart", task_type=raid_service.GAME_TYPE, item_id=root.id,
        ).pack(),
    )
    b.button(
        text="🗓️ Programma",
        callback_data=EventCb(
            action="sched", task_type=raid_service.GAME_TYPE, item_id=root.id,
        ).pack(),
    )
    b.adjust(1)
    note = (
        "\n🛟 Gemini non era disponibile: ho usato lo scenario di riserva collaudato."
        if fallback else ""
    )
    try:
        await wait_message.edit_text(
            f"✅ <b>Raid #{root.id} · {esc(root.title)}</b> pronto.{note}\n\n"
            "Puoi avviarlo subito: durante il test troverai anche "
            "<b>Risolvi fase ora</b> in /eventi.",
            reply_markup=b.as_markup(),
        )
    except Exception:  # noqa: BLE001 — harmless if the temporary message vanished
        await message.answer(
            f"✅ <b>Raid #{root.id} · {esc(root.title)}</b> pronto.{note}",
            reply_markup=b.as_markup(),
        )


def _hp_bar(hp: int) -> str:
    filled = max(0, min(10, (hp * 10 + raid_service.MAX_HP - 1) // raid_service.MAX_HP))
    return "🟥" * filled + "⬛" * (10 - filled)


def _phase_result(snapshot: RaidSnapshot, turn) -> list[str]:
    value = json.loads(turn.output_json)
    phase_no = int(value.get("phase", 0))
    if not 1 <= phase_no <= len(snapshot.blueprint.phases):
        return []
    phase = snapshot.blueprint.phases[phase_no - 1]
    if value.get("outcome") == "empty":
        return [f"▫️ Fase {phase_no}: nessuna risposta"]
    outcome = value.get("outcome")
    icon = "💥" if outcome == "decisive" else "✅" if outcome == "success" else "⚠️"
    counts = value.get("counts") if isinstance(value.get("counts"), dict) else {}
    chosen = " · ".join(
        f"{esc(phase.choices[key])} {int(counts.get(key, 0))}"
        for key in raid_service.TACTICS
    )
    narrative = phase.success_text if outcome in {"decisive", "success"} else phase.setback_text
    return [
        f"{icon} <b>Fase {phase_no}</b> · -{int(value.get('damage', 0))} HP "
        f"· {int(value.get('participants', 0))} partecipanti",
        f"<i>{esc(narrative)}</i>",
        f"🎯 Tattica efficace: {esc(phase.choices[phase.counter])}",
        f"Scelte: {chosen}",
    ]


def render_card(
    snapshot: RaidSnapshot, *, open_preview: bool = False,
) -> tuple[str, InlineKeyboardMarkup | None]:
    root, game, blueprint = snapshot.session, snapshot.game, snapshot.blueprint
    lines = [
        f"🐉 <b>{esc(root.title)}</b>",
        f"👹 <b>{esc(blueprint.boss_name)}</b>",
        f"{_hp_bar(game.boss_hp)} <b>{game.boss_hp}/{raid_service.MAX_HP} HP</b>",
    ]
    if not snapshot.turns:
        lines.extend(["", esc(blueprint.intro)])
    if snapshot.turns:
        lines.extend(["", "<b>Cronaca</b>"])
        for turn in snapshot.turns:
            lines.extend(_phase_result(snapshot, turn))

    keyboard: InlineKeyboardMarkup | None = None
    if root.status == "running" or open_preview:
        phase = blueprint.phases[game.current_phase - 1]
        lines.extend([
            "",
            f"<b>Fase {game.current_phase}/{raid_service.MAX_PHASES} · {esc(phase.title)}</b>",
            esc(phase.scene),
            f"🔎 <i>{esc(phase.telegraph)}</i>",
        ])
        if game.phase_deadline is not None:
            deadline = schedule_service.to_local(game.phase_deadline).strftime("%d/%m %H:%M")
            lines.append(f"⏳ Scelte aperte fino al <b>{deadline}</b>")
        lines.extend([
            "",
            "Scegli una tattica. Puoi cambiarla finché la fase è aperta; "
            "chi arriva tardi può entrare senza penalità.",
        ])
        b = InlineKeyboardBuilder()
        for tactic in raid_service.TACTICS:
            b.button(
                text=phase.choices[tactic],
                callback_data=RaidCb(
                    action="vote", session_id=root.id,
                    phase_no=game.current_phase, tactic=tactic,
                ).pack(),
            )
        # One full-width row per tactic: readable on phones and no accidental tap
        # caused by three compressed buttons with story-specific labels.
        b.adjust(1)
        keyboard = b.as_markup()
    elif game.result:
        lines.append("")
        if game.result == "victory":
            lines.extend(["🏆 <b>VITTORIA</b>", esc(blueprint.victory_text)])
        elif game.result == "defeat":
            lines.extend(["🌑 <b>IL BOSS RESISTE</b>", esc(blueprint.defeat_text)])
        else:
            lines.extend([
                "🌫️ <b>SPEDIZIONE CONCLUSA</b>",
                "Il gruppo si ritira senza penalità. Il boss potrà tornare in un altro racconto.",
            ])
        lines.append(f"👥 Avventurieri unici: <b>{snapshot.total_participants}</b>")
    return "\n".join(lines), keyboard


async def refresh_group_card(
    bot, db_session: AsyncSession, snapshot: RaidSnapshot,
) -> bool:
    text, keyboard = render_card(snapshot)
    try:
        await bot.edit_message_text(
            chat_id=snapshot.session.group_id,
            message_id=snapshot.session.anchor_message_id,
            text=text,
            reply_markup=keyboard,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — recover a deleted/non-editable anchor
        # A delayed internal retry may race with a newer successful update. In
        # that case Telegram reports "message is not modified": the card is
        # already correct, so sending a duplicate would be the actual failure.
        if "message is not modified" in str(exc).casefold():
            return True
        log.warning("Card raid #%s non editabile: %s", snapshot.session.id, exc)
        try:
            sent = await group_registry.send_group_message(
                bot, db_session, text, reply_markup=keyboard,
            )
            await raid_service.move_anchor(
                db_session, snapshot.session.id, sent.message_id,
                group_id=group_registry.get_group_id(),
            )
            return True
        except Exception as recovery_exc:  # noqa: BLE001 — caller persists a retry task
            log.error(
                "Recupero card raid #%s fallito: %s",
                snapshot.session.id, recovery_exc,
            )
            return False


@router.callback_query(
    RaidCb.filter(F.action == "vote"),
    F.message.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
)
async def vote(
    callback: CallbackQuery, callback_data: RaidCb, db_session: AsyncSession,
) -> None:
    if callback.message.chat.id != group_registry.get_group_id():
        await callback.answer("Questo raid appartiene a un altro gruppo.", show_alert=True)
        return
    ok, label = await raid_service.record_action(
        db_session,
        session_id=callback_data.session_id,
        phase_no=callback_data.phase_no,
        user_tg_id=callback.from_user.id,
        tactic=callback_data.tactic,
    )
    if not ok:
        await db_session.rollback()
        await callback.answer("Questa fase è già conclusa.", show_alert=True)
        return
    await db_session.commit()
    # No card edit per click: on a large group it would create a Bot API storm and
    # reveal a live bandwagon. The personal toast is immediate; totals are shown
    # together with the phase result.
    await callback.answer(f"✅ Scelta registrata: {label}")
