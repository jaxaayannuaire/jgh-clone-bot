"""
wizard_engine.py — Moteur conversationnel générique (style Typeform) pour
Telegram. Exécute des wizards définis de façon DÉCLARATIVE.

Principe : séparer le "quoi" (la définition d'un wizard = liste d'étapes) du
"comment" (ce moteur, écrit une fois, réutilisé partout).

Une commande = une intention. Les paramètres sont demandés progressivement.
Les choix fréquents sont des boutons. Les valeurs ont des défauts. Les données
sont contrôlées immédiatement. Un récapitulatif précède toute écriture.
VALIDER est la seule action qui déclenche l'exécution ; ANNULER n'écrit rien.

Ce module ne dépend pas de python-telegram-bot directement pour rester testable :
il produit des "vues" (texte + boutons) que la couche bot rend sur Telegram.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("jgh_clone.wizard.engine")


# ---------------------------------------------------------------------------
# Définition déclarative d'une étape
# ---------------------------------------------------------------------------
#
# Types d'étapes :
#   - "choice"  : question + boutons prédéfinis (options)
#   - "text"    : saisie libre (avec validation optionnelle)
#   - "confirm" : récapitulatif + VALIDER / MODIFIER / ANNULER
#
# Une étape est un dict. Champs communs :
#   key        : identifiant de la donnée collectée (obligatoire)
#   type       : "choice" | "text" | "confirm"
#   question   : texte affiché (ou fonction data -> texte)
#
# Spécifiques "choice" :
#   options    : liste de {"label","value"} OU fonction data -> liste
#   default    : valeur par défaut (proposée en bouton distinct)
#
# Spécifiques "text" :
#   optional      : True si la saisie peut être vide
#   default_from  : fonction data -> valeur par défaut si saisie vide
#   validate      : fonction (valeur, data) -> (ok: bool, message: str|None)
#   transform     : fonction (valeur, data) -> valeur normalisée
#
# Spécifiques "confirm" :
#   summary    : fonction data -> texte de récapitulatif


def resolve(value_or_fn, data: dict):
    """Si value_or_fn est appelable, l'appelle avec data ; sinon le renvoie."""
    if callable(value_or_fn):
        return value_or_fn(data)
    return value_or_fn


# ---------------------------------------------------------------------------
# Définition d'un wizard
# ---------------------------------------------------------------------------

@dataclass
class Wizard:
    type: str                          # identifiant ('demo', 'deployer', ...)
    title: str                         # titre affiché en tête
    steps: list[dict]                  # liste ordonnée d'étapes
    execute: Optional[Callable] = None # async fn(data, ctx) appelée au VALIDER
    intro: str = ""                    # texte d'accroche optionnel

    def step_by_index(self, idx: int) -> Optional[dict]:
        if 0 <= idx < len(self.steps):
            return self.steps[idx]
        return None

    def index_of(self, step_key: str) -> int:
        for i, s in enumerate(self.steps):
            if s["key"] == step_key:
                return i
        return -1


# ---------------------------------------------------------------------------
# Vue (ce que la couche bot doit afficher)
# ---------------------------------------------------------------------------

@dataclass
class WizardView:
    """Représentation neutre d'un écran de wizard (texte + boutons)."""
    text: str
    buttons: list[list[dict]] = field(default_factory=list)  # lignes de {label, data}
    expect_text: bool = False           # True si on attend une saisie libre


# ---------------------------------------------------------------------------
# Boutons de contrôle communs
# ---------------------------------------------------------------------------

def _control_row(session_id: int, *, with_back: bool, with_skip: bool = False):
    """Ligne de boutons de contrôle (retour / annuler / passer)."""
    row = []
    if with_back:
        row.append({"label": "◀️ Retour", "data": f"wiz:{session_id}:back"})
    if with_skip:
        row.append({"label": "⏭️ Passer", "data": f"wiz:{session_id}:skip"})
    row.append({"label": "❌ Annuler", "data": f"wiz:{session_id}:cancel"})
    return row


# ---------------------------------------------------------------------------
# Construction de la vue d'une étape
# ---------------------------------------------------------------------------

def build_step_view(wizard: Wizard, step_index: int, session_id: int,
                    data: dict) -> WizardView:
    """Construit la vue à afficher pour l'étape courante."""
    step = wizard.step_by_index(step_index)
    if step is None:
        return WizardView(text="⚠️ Étape introuvable.")

    stype = step["type"]
    is_first = (step_index == 0)
    header = f"*{wizard.title}*\n"
    progress = f"_Étape {step_index + 1}/{len(wizard.steps)}_\n\n"

    # --- CHOICE : question + boutons d'options ---
    if stype == "choice":
        question = resolve(step["question"], data)
        options = resolve(step.get("options", []), data)
        default = step.get("default")

        buttons = []
        for opt in options:
            label = opt["label"]
            val = opt["value"]
            # Marquer le défaut visuellement
            if default is not None and val == default:
                label = f"⭐ {label}"
            buttons.append([{
                "label": label,
                "data": f"wiz:{session_id}:choose:{step['key']}:{val}",
            }])
        buttons.append(_control_row(session_id, with_back=not is_first))

        return WizardView(text=header + progress + question, buttons=buttons)

    # --- TEXT : saisie libre ---
    if stype == "text":
        question = resolve(step["question"], data)
        hint = ""
        if step.get("optional"):
            hint = "\n\n_Tu peux laisser vide et appuyer sur « Passer »._"
        buttons = [_control_row(session_id, with_back=not is_first,
                                with_skip=step.get("optional", False))]
        return WizardView(text=header + progress + question + hint,
                          buttons=buttons, expect_text=True)

    # --- CONFIRM : récapitulatif + VALIDER / MODIFIER / ANNULER ---
    if stype == "confirm":
        summary = resolve(step["summary"], data)
        text = (header + "🔎 *Récapitulatif*\n\n" + summary +
                "\n\n⚠️ La validation déclenche l'opération.")
        buttons = [
            [{"label": "✅ VALIDER", "data": f"wiz:{session_id}:validate"}],
            [{"label": "✏️ Modifier", "data": f"wiz:{session_id}:edit"}],
            [{"label": "❌ Annuler", "data": f"wiz:{session_id}:cancel"}],
        ]
        return WizardView(text=text, buttons=buttons)

    return WizardView(text="⚠️ Type d'étape inconnu.")


# ---------------------------------------------------------------------------
# Validation d'une saisie texte
# ---------------------------------------------------------------------------

def validate_text_input(step: dict, raw: str, data: dict) -> tuple[bool, Any, Optional[str]]:
    """
    Valide/normalise une saisie texte pour une étape 'text'.
    Renvoie (ok, valeur_normalisée, message_erreur).
    """
    raw = (raw or "").strip()

    # Saisie vide
    if not raw:
        if step.get("optional"):
            # Valeur par défaut calculée, ou chaîne vide
            default_fn = step.get("default_from")
            value = default_fn(data) if default_fn else ""
            return (True, value, None)
        return (False, None, "Ce champ est obligatoire. Merci de saisir une valeur.")

    # Transformation (ex: slugify)
    transform = step.get("transform")
    value = transform(raw, data) if transform else raw

    # Validation métier
    validate = step.get("validate")
    if validate:
        ok, msg = validate(value, data)
        if not ok:
            return (False, None, msg or "Valeur invalide.")

    return (True, value, None)


# ---------------------------------------------------------------------------
# Construction de la vue "Modifier" (choix du champ à corriger)
# ---------------------------------------------------------------------------

def build_edit_view(wizard: Wizard, session_id: int, data: dict) -> WizardView:
    """Liste les champs déjà renseignés pour en choisir un à modifier."""
    buttons = []
    for i, step in enumerate(wizard.steps):
        if step["type"] == "confirm":
            continue
        key = step["key"]
        if key in data:
            label = step.get("edit_label", key)
            current = data.get(key)
            shown = current if current not in (None, "") else "(vide)"
            buttons.append([{
                "label": f"✏️ {label} : {shown}",
                "data": f"wiz:{session_id}:goto:{i}",
            }])
    buttons.append([{"label": "◀️ Retour au récapitulatif",
                     "data": f"wiz:{session_id}:tosummary"}])
    return WizardView(text="✏️ *Quel champ modifier ?*", buttons=buttons)
