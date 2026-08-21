"""
wizard_runtime.py — Couche d'exécution des wizards sur Telegram.

Relie le moteur générique (wizard_engine) au store de sessions (wizard_store)
et à python-telegram-bot. Gère :
  - le démarrage d'un wizard (/deployer, /demo, ...) ;
  - la reprise d'une session existante (option a : reprendre / abandonner) ;
  - le routage des callbacks 'wiz:<session>:<action>...' ;
  - la capture des saisies texte quand une étape 'text' est active ;
  - le rendu des vues (édition d'un message unique = fil du wizard).

Les wizards concrets sont enregistrés dans un REGISTRY {type: Wizard}.
"""

from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from wizard_engine import (
    Wizard, WizardView, build_step_view, build_edit_view, validate_text_input,
)

logger = logging.getLogger("jgh_clone.wizard.runtime")


# Registre des wizards disponibles (rempli par le bot au démarrage).
REGISTRY: dict[str, Wizard] = {}


def register_wizard(wizard: Wizard):
    REGISTRY[wizard.type] = wizard


# ---------------------------------------------------------------------------
# Helpers de rendu
# ---------------------------------------------------------------------------

def _to_markup(view: WizardView) -> Optional[InlineKeyboardMarkup]:
    if not view.buttons:
        return None
    rows = []
    for line in view.buttons:
        rows.append([InlineKeyboardButton(b["label"], callback_data=b["data"])
                     for b in line])
    return InlineKeyboardMarkup(rows)


async def _render(context, chat_id: int, session_id: int, view: WizardView,
                  store, edit_message_id: Optional[int] = None):
    """Affiche une vue : édite le message du fil si possible, sinon en crée un."""
    markup = _to_markup(view)
    if edit_message_id:
        try:
            await context.bot.edit_message_text(
                view.text, chat_id=chat_id, message_id=edit_message_id,
                reply_markup=markup, parse_mode="Markdown")
            return edit_message_id
        except Exception:
            pass  # message trop vieux/identique → on en envoie un nouveau
    msg = await context.bot.send_message(
        chat_id, view.text, reply_markup=markup, parse_mode="Markdown")
    store.set_message_id(session_id, msg.message_id)
    return msg.message_id


# ---------------------------------------------------------------------------
# Démarrage d'un wizard
# ---------------------------------------------------------------------------

async def start_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       wizard_type: str, initial_data: dict = None):
    """Point d'entrée d'une commande /xxx qui lance un wizard.

    initial_data : contexte pré-rempli (ex. liste d'options calculée par la
    commande au démarrage). Stocké dans la session dès sa création.
    """
    store = context.bot_data["wizard_store"]
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    wizard = REGISTRY.get(wizard_type)
    if not wizard:
        await update.message.reply_text("⚠️ Wizard inconnu.")
        return

    # Une session déjà active ? (option a : proposer reprendre / abandonner)
    existing = store.get_active_session(user_id)
    if existing:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "▶️ Reprendre", callback_data=f"wiz:{existing['id']}:resume")],
            [InlineKeyboardButton(
                "🆕 Abandonner et recommencer",
                callback_data=f"wiz:{existing['id']}:restart:{wizard_type}")],
        ])
        await update.message.reply_text(
            f"⏳ Tu as déjà un assistant en cours "
            f"(*{REGISTRY.get(existing['wizard_type'], wizard).title}*).\n"
            f"Que veux-tu faire ?",
            reply_markup=kb, parse_mode="Markdown")
        return

    await _begin_new_session(context, chat_id, user_id, wizard, store,
                             initial_data=initial_data)


async def _begin_new_session(context, chat_id, user_id, wizard, store,
                             reply_to=None, initial_data=None):
    first_step = wizard.steps[0]
    session_id = store.create_session(
        user_id, chat_id, wizard.type, first_step["key"],
        initial_data=initial_data)
    view = build_step_view(wizard, 0, session_id, initial_data or {})
    markup = _to_markup(view)
    intro = (wizard.intro + "\n\n") if wizard.intro else ""
    msg = await context.bot.send_message(
        chat_id, intro + view.text, reply_markup=markup, parse_mode="Markdown")
    store.set_message_id(session_id, msg.message_id)


# ---------------------------------------------------------------------------
# Routage des callbacks 'wiz:...'
# ---------------------------------------------------------------------------

async def on_wizard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    store = context.bot_data["wizard_store"]
    parts = query.data.split(":")
    # format: wiz:<session_id>:<action>[:<extra>...]
    if len(parts) < 3:
        return
    session_id = int(parts[1])
    action = parts[2]
    extra = parts[3:]

    sess = store.get_session(session_id)
    if not sess:
        await _safe_edit(query, "⚠️ Session introuvable.")
        return

    # Autorisation : seul le propriétaire de la session agit dessus
    if query.from_user.id != sess["user_id"]:
        await query.answer("Cette session ne t'appartient pas.", show_alert=True)
        return

    # --- Reprise / abandon (depuis l'écran "session en cours") ---
    if action == "resume":
        wizard = REGISTRY.get(sess["wizard_type"])
        if not wizard or sess["status"] != "active":
            await _safe_edit(query, "⚠️ Session expirée ou terminée.")
            return
        view = build_step_view(wizard, sess["step_index"], session_id,
                               sess["collected_data"])
        await _render(context, sess["chat_id"], session_id, view, store,
                      edit_message_id=query.message.message_id)
        return

    if action == "restart":
        store.cancel_session(session_id)
        new_type = extra[0] if extra else sess["wizard_type"]
        wizard = REGISTRY.get(new_type)
        if not wizard:
            await _safe_edit(query, "⚠️ Wizard inconnu.")
            return
        await _safe_edit(query, "🆕 Nouvel assistant démarré.")
        await _begin_new_session(context, sess["chat_id"], sess["user_id"],
                                 wizard, store)
        return

    # Pour la suite, la session doit être active
    if sess["status"] != "active":
        await _safe_edit(query, "⚠️ Cette session n'est plus active.")
        return

    wizard = REGISTRY.get(sess["wizard_type"])
    if not wizard:
        await _safe_edit(query, "⚠️ Wizard inconnu.")
        return

    data = sess["collected_data"]

    # --- Annulation ---
    if action == "cancel":
        store.cancel_session(session_id)
        await _safe_edit(query, "❌ Assistant annulé. Aucune modification effectuée.")
        return

    # --- Choix d'une option (choice) ---
    if action == "choose":
        step_key, value = extra[0], extra[1]
        store.save_answer(session_id, step_key, value)
        # Hook optionnel on_answer : permet à l'étape d'enrichir les données
        # (ex. résoudre l'instance choisie → stocker son type/nom/domaine).
        step = wizard.step_by_index(sess["step_index"])
        on_answer = step.get("on_answer") if step else None
        if on_answer:
            # Recharger data avec la réponse fraîche
            fresh = store.get_session(session_id)["collected_data"]
            enrich = on_answer(value, fresh)
            if enrich:
                for k, v in enrich.items():
                    store.save_answer(session_id, k, v)
        await _advance(context, query, wizard, session_id, store)
        return

    # --- Passer (text optionnel) ---
    if action == "skip":
        step = wizard.step_by_index(sess["step_index"])
        if step and step["type"] == "text":
            default_fn = step.get("default_from")
            value = default_fn(data) if default_fn else ""
            store.save_answer(session_id, step["key"], value)
            await _advance(context, query, wizard, session_id, store)
        return

    # --- Retour à l'étape précédente ---
    if action == "back":
        new_index = max(0, sess["step_index"] - 1)
        prev = wizard.step_by_index(new_index)
        store.goto_step(session_id, prev["key"], new_index)
        view = build_step_view(wizard, new_index, session_id, data)
        await _render(context, sess["chat_id"], session_id, view, store,
                      edit_message_id=query.message.message_id)
        return

    # --- Aller à une étape précise (depuis "Modifier") ---
    if action == "goto":
        idx = int(extra[0])
        step = wizard.step_by_index(idx)
        store.goto_step(session_id, step["key"], idx)
        view = build_step_view(wizard, idx, session_id, data)
        await _render(context, sess["chat_id"], session_id, view, store,
                      edit_message_id=query.message.message_id)
        return

    # --- Écran "Modifier" ---
    if action == "edit":
        view = build_edit_view(wizard, session_id, data)
        await _render(context, sess["chat_id"], session_id, view, store,
                      edit_message_id=query.message.message_id)
        return

    # --- Retour au récapitulatif ---
    if action == "tosummary":
        idx = _confirm_index(wizard)
        store.goto_step(session_id, wizard.steps[idx]["key"], idx)
        view = build_step_view(wizard, idx, session_id, data)
        await _render(context, sess["chat_id"], session_id, view, store,
                      edit_message_id=query.message.message_id)
        return

    # --- VALIDER (seule action qui écrit) ---
    if action == "validate":
        # Anti-double-validation atomique
        if not store.claim_completion(session_id):
            await _safe_edit(query, "⚠️ Déjà validé (ou session terminée).")
            return
        await _safe_edit(query, "✅ Validé. Traitement en cours…")
        if wizard.execute:
            try:
                await wizard.execute(data, {
                    "context": context, "chat_id": sess["chat_id"],
                    "user_id": sess["user_id"], "session_id": session_id,
                })
            except Exception as e:
                logger.exception("Erreur d'exécution du wizard %s", wizard.type)
                await context.bot.send_message(
                    sess["chat_id"], f"🔴 Erreur pendant l'exécution : {e}")
        return


# ---------------------------------------------------------------------------
# Capture des saisies texte (étapes 'text')
# ---------------------------------------------------------------------------

async def on_wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Si l'utilisateur a une session active sur une étape 'text', traite sa
    saisie. Renvoie True si la saisie a été consommée par un wizard, False
    sinon (pour laisser d'autres handlers agir).
    """
    store = context.bot_data.get("wizard_store")
    if store is None:
        return False
    user_id = update.effective_user.id
    sess = store.get_active_session(user_id)
    if not sess:
        return False

    wizard = REGISTRY.get(sess["wizard_type"])
    if not wizard:
        return False
    step = wizard.step_by_index(sess["step_index"])
    if not step or step["type"] != "text":
        return False

    raw = update.message.text or ""
    ok, value, err = validate_text_input(step, raw, sess["collected_data"])
    if not ok:
        await update.message.reply_text(f"⚠️ {err}")
        return True  # consommé (on attend une nouvelle saisie)

    store.save_answer(sess["id"], step["key"], value)
    # Avancer à l'étape suivante
    next_index = sess["step_index"] + 1
    await _advance_from_text(context, update, wizard, sess["id"], store, next_index)
    return True


# ---------------------------------------------------------------------------
# Navigation interne
# ---------------------------------------------------------------------------

def _next_visible_index(wizard, from_index: int, data: dict) -> int:
    """Renvoie l'index de la prochaine étape VISIBLE à partir de from_index.

    Une étape est sautée si elle définit skip_if(data) -> True. Permet des
    étapes conditionnelles (ex. confirmation par nom seulement pour les
    clients). S'arrête au dernier index si tout est sauté."""
    idx = from_index
    while idx < len(wizard.steps):
        step = wizard.steps[idx]
        skip_if = step.get("skip_if")
        if skip_if and skip_if(data):
            idx += 1
            continue
        return idx
    return len(wizard.steps) - 1


async def _advance(context, query, wizard, session_id, store):
    """Avance à l'étape suivante VISIBLE après un clic (choice/skip)."""
    sess = store.get_session(session_id)
    next_index = _next_visible_index(
        wizard, sess["step_index"] + 1, sess["collected_data"])
    step = wizard.step_by_index(next_index)
    store.goto_step(session_id, step["key"], next_index)
    view = build_step_view(wizard, next_index, session_id,
                           sess["collected_data"])
    await _render(context, sess["chat_id"], session_id, view, store,
                  edit_message_id=query.message.message_id)


async def _advance_from_text(context, update, wizard, session_id, store,
                             next_index):
    """Avance après une saisie texte (nouveau message, pas d'édition)."""
    sess = store.get_session(session_id)
    next_index = _next_visible_index(wizard, next_index, sess["collected_data"])
    step = wizard.step_by_index(next_index)
    store.goto_step(session_id, step["key"], next_index)
    view = build_step_view(wizard, next_index, session_id,
                           sess["collected_data"])
    markup = _to_markup(view)
    msg = await update.message.reply_text(
        view.text, reply_markup=markup, parse_mode="Markdown")
    store.set_message_id(session_id, msg.message_id)


def _confirm_index(wizard: Wizard) -> int:
    for i, s in enumerate(wizard.steps):
        if s["type"] == "confirm":
            return i
    return len(wizard.steps) - 1


async def _safe_edit(query, text: str):
    try:
        await query.edit_message_text(text, parse_mode="Markdown")
    except Exception:
        try:
            await query.message.reply_text(text, parse_mode="Markdown")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tâche périodique d'expiration
# ---------------------------------------------------------------------------

async def expire_sessions_job(context: ContextTypes.DEFAULT_TYPE):
    store = context.bot_data.get("wizard_store")
    if store:
        n = store.expire_stale()
        if n:
            logger.info("Wizard : %d session(s) expirée(s)", n)
