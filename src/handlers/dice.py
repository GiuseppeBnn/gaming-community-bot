"""Tiny, provider-free dice commands."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from utils import dice

router = Router(name="dice")


@router.message(Command("d20"))
async def cmd_d20(message: Message) -> None:
    # Intentionally only the number: this is meant to be frictionless in chat.
    await message.answer(str(dice.d20()))
