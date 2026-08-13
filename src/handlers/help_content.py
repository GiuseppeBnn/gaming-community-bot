"""
Command registry + help rendering — single source of truth for ``/comandi``
(the grouped legend) and ``/spiega_comando <comando>`` (a per-command manual).

Keeping the catalog here (instead of scattering descriptions across handlers)
lets both the short legend and the detailed "man pages" stay in sync. ``details``
bodies carry intentional HTML markup (``<b>``, ``<code>``, …); ``usage`` strings
are literal command syntax whose ``<placeholder>`` tokens must be HTML-escaped at
render time (otherwise Telegram reads e.g. ``<importo>`` as an unknown open tag
and rejects the whole message). The user-controlled looked-up command name is
escaped by the caller.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from config_data.config import settings
from utils.text import esc


@dataclass(frozen=True)
class CommandDoc:
    name: str                       # command without the leading slash
    summary: str                    # one-line description (shown in the legend)
    category: str                   # grouping bucket
    usage: str = ""                 # e.g. "/trasferisci @utente <importo>"
    details: str = ""               # novice-friendly manual body (HTML)
    admin_only: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)


# Category order for the legend (user buckets first, then admin buckets).
USER_CATEGORIES = (
    "👤 Profilo & Economia",
    "🎲 Scommesse",
    "🏆 Progressione",
    "🍺 Locanda",
    "🎲 Giochi rapidi",
    "🤖 Intrattenimento AI",
    "❓ Aiuto",
)
ADMIN_CATEGORIES = (
    "🔧 Pannelli",
    "💰 Valuta & XP",
    "🛡️ Moderazione",
    "📊 Info",
    "🎬 Eventi",
)

_COMMANDS: list[CommandDoc] = [
    # --- 👤 Profilo & Economia ---
    CommandDoc(
        "start", "Avvia il bot e apri il menu principale", "👤 Profilo & Economia",
        usage="/start",
        details="Registra il tuo profilo (la prima volta) e mostra il menu. È anche il "
                "comando che apre le schermate private quando tocchi un pulsante nel gruppo.",
    ),
    CommandDoc(
        "profilo", "Il tuo profilo: XP, saldo, trofei, tag", "👤 Profilo & Economia",
        usage="/profilo",
        details="Mostra la tua scheda personale: rango XP, CoInn 🪙, numero di trofei e "
                "i tag attivi. Funziona anche nel gruppo (mostra sempre e solo il tuo profilo).",
    ),
    CommandDoc(
        "saldo", "Saldo e ultimi movimenti", "👤 Profilo & Economia",
        usage="/saldo",
        details="Mostra quanti CoInn 🪙 hai e le ultime transazioni (premi, scommesse, "
                "acquisti, trasferimenti).",
    ),
    CommandDoc(
        "storico", "Cronologia completa dei movimenti", "👤 Profilo & Economia",
        usage="/storico",
        details="L'elenco esteso di tutte le tue entrate e uscite di CoInn, dalla più recente.",
    ),
    CommandDoc(
        "daily", "Premio giornaliero (si azzera a mezzanotte)", "👤 Profilo & Economia",
        usage="/daily",
        details="Riscuoti il premio in CoInn: <b>uno al giorno</b>, e il giorno si azzera a "
                "<b>mezzanotte</b>. Se hai riscosso a tarda notte devono comunque passare almeno "
                f"<b>{settings.daily_min_hours} ore</b> dall'ultima volta. Se riprovi prima, il "
                "bot ti dice quanto manca. Riscuoterlo in giorni consecutivi aumenta la tua "
                "<i>streak</i>: <b>saltare un giorno la azzera</b>.",
    ),
    CommandDoc(
        "trasferisci", "Manda CoInn a un altro utente", "👤 Profilo & Economia",
        usage="/trasferisci @utente <importo>",
        details="Trasferisce CoInn dal tuo saldo a quello di un altro membro. Puoi indicare "
                "il destinatario con @username, rispondendo a un suo messaggio, o col suo ID.\n"
                "Esempio: <code>/trasferisci @mario 100</code>.",
    ),
    # --- 🎲 Scommesse ---
    CommandDoc(
        "scommesse", "Vedi le scommesse aperte e gioca", "🎲 Scommesse",
        usage="/scommesse",
        details="Elenca gli eventi su cui puoi scommettere. Scegli un evento, un'opzione e "
                "l'importo: se indovini, vinci in proporzione alla quota.",
    ),
    CommandDoc(
        "crea_scommessa", "Proponi una nuova scommessa", "🎲 Scommesse",
        usage="/crea_scommessa",
        details="Avvia in chat privata la creazione guidata di una scommessa (titolo, "
                "descrizione e opzioni). La pubblicazione effettiva è gestita dagli admin.",
    ),
    # --- 🏆 Progressione ---
    CommandDoc(
        "quiz", "Gioca al quiz in corso (o vedi se ce n'è uno)", "🏆 Progressione",
        usage="/quiz",
        details="Se è in corso un quiz, ti dà il pulsante per <b>giocarlo</b> in chat privata; "
                "altrimenti ti dice chiaramente che non ce ne sono di attivi. Gli admin lo usano "
                "invece per gestire e avviare i quiz.",
    ),
    CommandDoc(
        "trofei", "I tuoi trofei (per rarità) e il rango", "🏆 Progressione",
        usage="/trofei",
        details="Mostra i trofei che hai sbloccato, raggruppati per rarità, e il tuo rango XP attuale.",
    ),
    CommandDoc(
        "catalogo_trofei", "Tutti i trofei ottenibili", "🏆 Progressione",
        usage="/catalogo_trofei",
        details="L'elenco completo dei trofei disponibili e di come si sbloccano.",
    ),
    CommandDoc(
        "classifiche", "Classifiche: ricchezza · XP · trofei", "🏆 Progressione",
        usage="/classifiche",
        details="Le classifiche della community con uno switcher tra ricchezza, XP e trofei. "
                "Si aprono in chat privata.",
    ),
    # --- 🍺 Locanda ---
    CommandDoc(
        "locanda", "La Locanda: tag cosmetici e menù consumabili", "🍺 Locanda",
        usage="/locanda",
        aliases=("negozio",),
        details="<i>La Locanda del Drago.</i> Spendi CoInn in due sezioni: 🏷️ "
                "<b>Personalizzazioni</b> (tag cosmetici da mostrare sul profilo, attivabili "
                "e combinabili) e 🍖 <b>Menù della Locanda</b> (cibi e bevande da acquistare "
                "più volte, che finiscono nella tua 🎒 Dispensa e sbloccano trofei).",
    ),
    # --- 🎲 Giochi rapidi ---
    CommandDoc(
        "d20", "Tira un dado da 1 a 20", "🎲 Giochi rapidi",
        usage="/d20",
        details="Alduino risponde con un solo numero casuale da <b>1</b> a <b>20</b>, inclusi.",
    ),
    # --- 🤖 Intrattenimento AI (in gruppo, in risposta a un messaggio) ---
    CommandDoc(
        "maestro", "Trasforma uno sfogo in filosofia aulica", "🤖 Intrattenimento AI",
        usage="/maestro (in risposta a un messaggio)",
        details="Rispondi a un messaggio con /maestro: l'AI lo riscrive in tono filosofico aulico.",
    ),
    CommandDoc(
        "complotto", "Teoria del complotto sul messaggio", "🤖 Intrattenimento AI",
        usage="/complotto (in risposta a un messaggio)",
        details="Genera una teoria del complotto ironica a partire dal messaggio a cui rispondi.",
    ),
    CommandDoc(
        "difendi", "Avvocato difensore del messaggio", "🤖 Intrattenimento AI",
        usage="/difendi (in risposta a un messaggio)",
        details="Un avvocato senza scrupoli inventa un'arringa difensiva (sempre diversa) "
                "per il messaggio a cui rispondi.",
    ),
    CommandDoc(
        "accusa", "Inquisitore: condanna il messaggio", "🤖 Intrattenimento AI",
        usage="/accusa (in risposta a un messaggio)",
        details="Un inquisitore condanna senza pietà il messaggio a cui rispondi.",
    ),
    CommandDoc(
        "drama", "Versione anime drammatica", "🤖 Intrattenimento AI",
        usage="/drama (in risposta a un messaggio)",
        details="Riscrive il messaggio come il climax di un anime drammatico.",
    ),
    CommandDoc(
        "dialetto", "Traduce in catanese stretto", "🤖 Intrattenimento AI",
        usage="/dialetto (in risposta a un messaggio)",
        details="Traduce il messaggio a cui rispondi in catanese stretto e autentico.",
    ),
    CommandDoc(
        "insulta", "Blasta l'utente taggato", "🤖 Intrattenimento AI",
        usage="/insulta @utente",
        details="L'AI confeziona un insulto originale e tagliente rivolto all'utente che tagghi. "
                "Roba da adulti, niente peli sulla lingua.",
    ),
    CommandDoc(
        "alduino", "Parla col drago Alduino", "🤖 Intrattenimento AI",
        usage="/alduino <messaggio>",
        details="Scrivi a <b>Alduino</b>, il draghetto viola gamer della community: gli parli "
                "direttamente e lui ti risponde a tono — gentile e premuroso, ma furbo e "
                "sarcastico quando ci sta. Es. <code>/alduino consigliami un gioco</code> "
                "(o rispondi a un messaggio).",
    ),
    # --- ❓ Aiuto ---
    CommandDoc(
        "comandi", "Mostra la guida ai comandi", "❓ Aiuto",
        usage="/comandi",
        details="Mostra l'elenco raggruppato dei comandi disponibili.",
        aliases=("help",),
    ),
    CommandDoc(
        "spiega_comando", "Spiegazione dettagliata di un comando", "❓ Aiuto",
        usage="/spiega_comando <comando>",
        details="Mostra la spiegazione estesa di un comando.\n"
                "Esempio: <code>/spiega_comando daily</code>. Funziona solo in chat privata.",
    ),

    # ===================== ADMIN =====================
    CommandDoc("admin", "Pannello admin (stats · eventi · utenti)", "🔧 Pannelli",
               usage="/admin", admin_only=True),
    CommandDoc("gestisci_scommesse", "Pannello gestione scommesse", "🔧 Pannelli",
               usage="/gestisci_scommesse", admin_only=True),
    CommandDoc("credita", "Accredita CoInn a un utente", "💰 Valuta & XP",
               usage="/credita @utente <n>", admin_only=True),
    CommandDoc("addebita", "Sottrai CoInn a un utente", "💰 Valuta & XP",
               usage="/addebita @utente <n>", admin_only=True),
    CommandDoc("setsaldo", "Imposta il saldo di un utente", "💰 Valuta & XP",
               usage="/setsaldo @utente <n>", admin_only=True),
    CommandDoc("airdrop", "Distribuisci CoInn a tutti", "💰 Valuta & XP",
               usage="/airdrop <n>", admin_only=True),
    CommandDoc("saldo_di", "Mostra il saldo di un utente", "💰 Valuta & XP",
               usage="/saldo_di @utente", admin_only=True),
    CommandDoc("dai_xp", "Assegna XP a un utente", "💰 Valuta & XP",
               usage="/dai_xp @utente <n>", admin_only=True),
    CommandDoc("set_xp", "Imposta l'XP di un utente", "💰 Valuta & XP",
               usage="/set_xp @utente <n>", admin_only=True),
    CommandDoc("ban", "Banna un utente dal gruppo", "🛡️ Moderazione",
               usage="/ban (reply o @utente/ID)", admin_only=True,
               details="Rimuove l'utente dal gruppo e lo rende <b>muto al bot</b>: non riceverà più "
                       "risposte da nessuna parte, nemmeno in privato. I suoi dati restano intatti; "
                       "<code>/sban</code> ripristina tutto."),
    CommandDoc("sban", "Revoca il ban", "🛡️ Moderazione",
               usage="/sban (reply o @utente/ID)", admin_only=True),
    CommandDoc("kick", "Espelli un utente", "🛡️ Moderazione",
               usage="/kick (reply o @utente/ID)", admin_only=True),
    CommandDoc("mute", "Silenzia un utente", "🛡️ Moderazione",
               usage="/mute [10m] (reply o @utente/ID)", admin_only=True),
    CommandDoc("unmute", "Riattiva un utente silenziato", "🛡️ Moderazione",
               usage="/unmute (reply o @utente/ID)", admin_only=True),
    CommandDoc("warn", "Ammonisci un utente", "🛡️ Moderazione",
               usage="/warn [motivo] (reply o @utente/ID)", admin_only=True),
    CommandDoc("warns", "Elenca i warn attivi di un utente", "🛡️ Moderazione",
               usage="/warns (reply o @utente/ID)", admin_only=True),
    CommandDoc("unwarn", "Rimuove un warn a un utente", "🛡️ Moderazione",
               usage="/unwarn (reply o @utente/ID)", admin_only=True),
    CommandDoc("info", "Dossier completo di un utente", "📊 Info",
               usage="/info (reply o @utente/ID)", admin_only=True),
    CommandDoc("cerca", "Cerca utenti per nome/username", "📊 Info",
               usage="/cerca <testo>", admin_only=True,
               details="Solo admin e solo in chat privata col bot."),
    CommandDoc("classifica", "Classifica ricchezza (admin)", "📊 Info",
               usage="/classifica", admin_only=True),
    CommandDoc("stats", "Statistiche della community", "📊 Info",
               usage="/stats", admin_only=True),
    CommandDoc("audit", "Registro azioni admin", "📊 Info",
               usage="/audit [utente]", admin_only=True,
               details="Solo admin e solo in chat privata col bot."),
    CommandDoc("lista_ranghi", "Sistema ranghi e livelli", "📊 Info",
               usage="/lista_ranghi", admin_only=True,
               details="Mostra la curva dei livelli (XP per salire, crescita %) e come i "
                       "nomi-rango (Novizio→Leggenda) si mappano sulle fasce di livello."),
    CommandDoc("eventi", "Hub eventi: quiz, Guess The Game, Sound Quest, sondaggi, scommesse",
               "🎬 Eventi",
               usage="/eventi", admin_only=True,
               details="Crea quiz, <b>Guess The Game</b> (indovina il gioco da una foto), "
                       "<b>Sound Quest</b> (dall'audio), sondaggi e scommesse, poi avviali "
                       "subito nel gruppo o programmali. È lo stesso hub raggiungibile da "
                       "/admin → 🎬 Eventi.\n\n"
                       "Nei due giochi «indovina» si gioca <b>in privato</b>: nel gruppo "
                       "arriva solo l'invito, mai l'immagine o l'audio (li rivela il podio "
                       "alla chiusura). Vince chi ci arriva in <b>meno tentativi</b>; a "
                       "parità conta il tempo. Le risposte libere le giudica l'AI, quindi "
                       "«GTA SA» vale «Grand Theft Auto: San Andreas» — ma la <b>serie da "
                       "sola non basta</b>. In creazione puoi aggiungere grafie alternative "
                       "sempre accettate: sono la rete di sicurezza se l'AI non risponde."),
    CommandDoc("crea_quiz", "Crea un quiz (in privato)", "🎬 Eventi",
               usage="/crea_quiz", admin_only=True),
    CommandDoc("quiz", "Elenca i quiz pronti e avviali", "🎬 Eventi",
               usage="/quiz", admin_only=True),
    CommandDoc("avvia_quiz", "Avvia subito un quiz", "🎬 Eventi",
               usage="/avvia_quiz <id>", admin_only=True),
    CommandDoc("chiudi_quiz", "Chiudi un quiz e pubblica il podio", "🎬 Eventi",
               usage="/chiudi_quiz <id>", admin_only=True),
    CommandDoc("sondaggio", "Crea un sondaggio", "🎬 Eventi",
               usage="/sondaggio", admin_only=True,
               details="Crea un sondaggio (domanda + opzioni) <b>in chat privata</b>. "
                       "Come quiz e scommesse, viene <b>salvato</b> e poi scegli se "
                       "<b>avviarlo subito</b> nel gruppo o <b>programmarlo</b> — non "
                       "viene pubblicato all'istante."),
    CommandDoc("programma", "Programma un evento (quiz/sondaggio/scommessa)", "🎬 Eventi",
               usage="/programma", admin_only=True),
    CommandDoc("programmati", "Elenca/annulla gli eventi programmati", "🎬 Eventi",
               usage="/programmati", admin_only=True),
]

# name (and alias) → CommandDoc
_BY_NAME: dict[str, CommandDoc] = {}
for _c in _COMMANDS:
    _BY_NAME[_c.name] = _c
    for _a in _c.aliases:
        _BY_NAME[_a] = _c


def normalize(raw: str) -> str:
    """Reduce a user-typed command to a bare lookup key (strip '/', lowercase)."""
    return raw.strip().lstrip("/").split("@")[0].strip().lower()


def lookup(raw: str) -> CommandDoc | None:
    return _BY_NAME.get(normalize(raw))


def suggestions(raw: str, limit: int = 3) -> list[str]:
    """Closest known command names to a mistyped one (for 'comando non trovato')."""
    return difflib.get_close_matches(normalize(raw), sorted(_BY_NAME), n=limit, cutoff=0.5)


def _by_category(categories: tuple[str, ...]) -> list[tuple[str, list[CommandDoc]]]:
    out: list[tuple[str, list[CommandDoc]]] = []
    for cat in categories:
        cmds = [c for c in _COMMANDS if c.category == cat]
        if cmds:
            out.append((cat, cmds))
    return out


def render_legend(is_admin: bool = False) -> str:
    """The grouped command legend shown by /comandi."""
    lines = [
        "📖 <b>Guida ai comandi</b>",
        "<i>Suggerimento:</i> <code>/spiega_comando &lt;comando&gt;</code> "
        "<i>per la spiegazione dettagliata.</i>",
    ]
    for cat, cmds in _by_category(USER_CATEGORIES):
        lines.append(f"\n<b>{cat}</b>")
        lines.extend(f"/{c.name} — {c.summary}" for c in cmds)
    if is_admin:
        lines.append("\n━━━━━━━━━━━━━━━━━━━━")
        lines.append("🔐 <b>Comandi Admin</b>")
        for cat, cmds in _by_category(ADMIN_CATEGORIES):
            lines.append(f"\n<b>{cat}</b>")
            lines.extend(f"/{c.name} — {c.summary}" for c in cmds)
    return "\n".join(lines)


def render_command(raw: str, is_admin: bool = False) -> str | None:
    """A detailed manual page for one command, or None if unknown/not visible.

    Admin-only commands are hidden from non-admins (treated as unknown) so the
    help surface never leaks the admin command set.
    """
    doc = lookup(raw)
    if doc is None or (doc.admin_only and not is_admin):
        return None
    parts = [f"📘 <b>/{doc.name}</b>", "", doc.summary]
    if doc.usage:
        # usage is literal command syntax: its <placeholder> tokens are not HTML
        # and must be escaped, or Telegram rejects the message (unknown tag).
        parts += ["", f"📋 <b>Uso:</b> <code>{esc(doc.usage)}</code>"]
    if doc.details:
        parts += ["", doc.details]
    if doc.admin_only:
        parts += ["", "<i>🔐 Comando riservato agli admin.</i>"]
    return "\n".join(parts)


def render_command_or_hint(raw: str, is_admin: bool = False) -> str:
    """The man page for ``raw``, or a 'not found' message with close suggestions.

    Shared by ``/spiega_comando`` (private) and the ``spiega_<cmd>`` deep-link, so
    the two surfaces stay identical.
    """
    page = render_command(raw, is_admin)
    if page is not None:
        return page
    hint = ""
    near = [s for s in suggestions(raw) if render_command(s, is_admin)]
    if near:
        hint = "\n\nForse cercavi: " + " · ".join(f"<code>/{s}</code>" for s in near)
    return f"❓ Comando «{esc(raw, 32)}» non trovato. Usa /comandi per l'elenco.{hint}"
