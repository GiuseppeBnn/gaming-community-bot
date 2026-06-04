from __future__ import annotations


class InsufficientFundsError(Exception):
    def __init__(self, balance: int, required: int) -> None:
        self.balance = balance
        self.required = required
        super().__init__(
            f"Saldo insufficiente: hai {balance} 🪙, servono {required} 🪙."
        )


class WalletNotFoundError(Exception):
    def __init__(self, tg_id: int) -> None:
        self.tg_id = tg_id
        super().__init__(f"Wallet non trovato per l'utente {tg_id}.")


class DailyAlreadyClaimedError(Exception):
    def __init__(self, hours_remaining: float) -> None:
        self.hours_remaining = hours_remaining
        super().__init__(
            f"Premio giornaliero già riscosso. Riprova tra {hours_remaining:.1f} ore."
        )


class SelfTransferError(Exception):
    def __init__(self) -> None:
        super().__init__("Non puoi trasferire coin a te stesso.")


class AlreadyBetError(Exception):
    def __init__(self, user_tg_id: int, event_id: int) -> None:
        self.user_tg_id = user_tg_id
        self.event_id = event_id
        super().__init__(
            f"L'utente {user_tg_id} ha già scommesso sull'evento #{event_id}."
        )


class BettingClosedError(Exception):
    def __init__(self, event_id: int, status: str) -> None:
        self.event_id = event_id
        self.status = status
        super().__init__(
            f"L'evento #{event_id} non accetta più scommesse (stato: {status})."
        )


class EventNotFoundError(Exception):
    def __init__(self, event_id: int) -> None:
        self.event_id = event_id
        super().__init__(f"Evento #{event_id} non trovato.")


class EventAlreadySettledError(Exception):
    def __init__(self, event_id: int, status: str) -> None:
        self.event_id = event_id
        self.status = status
        super().__init__(
            f"L'evento #{event_id} è già stato chiuso (stato: {status})."
        )
