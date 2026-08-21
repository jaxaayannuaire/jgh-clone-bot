"""
ui_render.py — Helpers de rendu pour un style « liste → détail » inspiré de
JGH Alert Bot : en-têtes, séparateurs, boutons 2 colonnes numérotés,
pagination. Réutilisable par /instances, /commandes et futures commandes.

Principe (cohérent entre les bots Jaxaay Group) :
  - un écran LISTE : en-tête + compteur + items numérotés + séparateurs,
    puis des boutons 2 colonnes « n • libellé » pour ouvrir le détail ;
  - un écran DÉTAIL : infos en pictogrammes + boutons d'action contextuels ;
  - navigation commune : ◀️ Retour, 🏠 Accueil, ♻️ Actualiser.

Ce module ne dépend pas de python-telegram-bot pour rester testable : il
produit du texte et des descripteurs de boutons ({label, data}) que la couche
bot convertit en InlineKeyboardMarkup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

# Séparateur horizontal (60 tirets), signature visuelle du style Alert Bot.
SEP = "—" * 30  # 30 em-dashes ≈ largeur agréable sur mobile
SEP_THIN = "-" * 60

PAGE_SIZE = 10  # lignes par page (demandé)


@dataclass
class ListItem:
    """Un item de liste : ce qui s'affiche + la valeur pour le bouton détail."""
    number: int                 # numéro de listing (1-based, sur la page)
    short_label: str            # libellé court du bouton (ex. nom)
    body: str                   # bloc d'infos affiché dans la liste
    value: str                  # valeur encodée dans le callback (ex. id)


@dataclass
class RenderedScreen:
    """Écran prêt à envoyer : texte + lignes de boutons ({label, data})."""
    text: str
    buttons: list = field(default_factory=list)


def paginate(items: list, page: int, page_size: int = PAGE_SIZE):
    """Découpe une liste en page. Renvoie (page_items, page, total_pages)."""
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start:start + page_size], page, total_pages


def build_list_screen(
    *, title: str, icon: str, total: int, page: int, total_pages: int,
    items: list[ListItem], detail_prefix: str,
    nav_prefix: str, columns: int = 2,
    counts_header: Optional[str] = None,
) -> RenderedScreen:
    """
    Construit un écran de liste style Alert Bot.

    - title/icon : en-tête (ex. icon='📦', title='Instances')
    - total, page, total_pages : pour le compteur « N résultat(s) — Page x/y »
    - items : les ListItem de la page courante
    - detail_prefix : préfixe callback pour ouvrir un détail
      (→ '<detail_prefix>:<value>')
    - nav_prefix : préfixe callback pour la navigation
      (→ '<nav_prefix>:page:<n>', '<nav_prefix>:home', '<nav_prefix>:refresh')
    - counts_header : bloc optionnel de compteurs par catégorie (multi-lignes)
    """
    lines = [f"{icon} *{title}*", SEP]
    if counts_header:
        lines.append(counts_header)
        lines.append(SEP)
    lines.append(f"{total} résultat(s) — Page {page}/{total_pages}")
    lines.append(SEP)

    for it in items:
        lines.append(f"*{it.number}.* {it.body}")
        lines.append(SEP)

    text = "\n".join(lines)

    # Boutons détail : 2 colonnes « n • libellé »
    buttons = []
    row = []
    for it in items:
        label = f"{it.number} • {it.short_label}"
        row.append({"label": label, "data": f"{detail_prefix}:{it.value}"})
        if len(row) == columns:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Ligne de pagination (◀️ / ▶️) si plusieurs pages
    pager = []
    if page > 1:
        pager.append({"label": "◀️ Précédent",
                      "data": f"{nav_prefix}:page:{page - 1}"})
    if page < total_pages:
        pager.append({"label": "Suivant ▶️",
                      "data": f"{nav_prefix}:page:{page + 1}"})
    if pager:
        buttons.append(pager)

    # Navigation commune
    buttons.append([
        {"label": "🏠 Accueil", "data": f"{nav_prefix}:home"},
        {"label": "♻️ Actualiser", "data": f"{nav_prefix}:refresh"},
    ])

    return RenderedScreen(text=text, buttons=buttons)


def build_detail_screen(
    *, title: str, icon: str, fields: list[tuple[str, str]],
    actions: list[dict], nav_prefix: str,
    footer: Optional[str] = None,
) -> RenderedScreen:
    """
    Construit un écran de détail style Alert Bot.

    - fields : liste de (picto+label, valeur) affichés en lignes
    - actions : boutons d'action contextuels [{label, data}], disposés 2/ligne
    - nav_prefix : pour ◀️ Retour aux résultats / 🏠 Accueil
    - footer : bloc optionnel en bas (ex. liste des sites liés)
    """
    lines = [f"{icon} *{title}*", SEP]
    for label, value in fields:
        lines.append(f"{label} : {value}")
    if footer:
        lines.append(SEP)
        lines.append(footer)
    text = "\n".join(lines)

    buttons = []
    # Actions contextuelles, 2 par ligne
    row = []
    for act in actions:
        row.append(act)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Navigation
    buttons.append([
        {"label": "◀️ Retour", "data": f"{nav_prefix}:back"},
        {"label": "🏠 Accueil", "data": f"{nav_prefix}:home"},
    ])

    return RenderedScreen(text=text, buttons=buttons)
