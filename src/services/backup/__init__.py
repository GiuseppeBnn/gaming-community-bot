"""
Backup & state-export subsystem (§25).

Two independent, opt-in, non-blocking facilities:

  * ``state_export`` — engine-agnostic logical dump/restore of the whole DB, so a
    migration (SQLite→Postgres, new host) never resets coins/XP/streaks/etc.
  * ``chat_archive`` — incremental, crash-safe archive of the group's text
    messages via MTProto/Telethon (the Bot API cannot read history).

``loop.backup_loop`` drives both on a cadence; both can also be triggered by the
admin handlers. Everything streams — nothing loads a full dataset into memory.
"""
