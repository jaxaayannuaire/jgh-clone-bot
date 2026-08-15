"""
bot.py — JGH Clone Bot (version minimale : flux provision).

Pilotage Telegram du provisioning Coolify. Reprend les patterns de JGH Alert
Bot : whitelist Telegram, actions sensibles sous confirmation inline ✅/❌,
connexion DuckDB unique, secrets hors repo (.env).

Flux /provision :
    1. /provision <nom> <domaine>  → calcule le plan (dry-run), crée un clone_job
    2. Récapitulatif + boutons ✅ Confirmer / ❌ Ignorer (admins only)
    3. ✅ → create app Coolify → deploy → suivi → statut 'active'
       ❌ → job 'failed' (annulé)

Périmètre volontairement minimal : déploie le repo de test (jgh-compose-test).
Le catalogue de packs, l'injection client Dolibarr et le transport des
documents viendront ensuite.
"""

from __future__ import annotations

import logging
import os
import time

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from db.duckdb_client import Database
from coolify_connector import (
    CoolifyConnector, CoolifyConfig,
    CoolifyError, CoolifyAuthError, CoolifyDomainConflict, CoolifyNotFound,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("jgh_clone.bot")


# ---------------------------------------------------------------------------
# Configuration & garde-fous d'accès
# ---------------------------------------------------------------------------

def _parse_ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}

ALLOWED_IDS = _parse_ids(os.environ.get("ALLOWED_TELEGRAM_IDS", ""))
ADMIN_IDS = _parse_ids(os.environ.get("ADMIN_TELEGRAM_IDS", ""))

# Suffixe de domaine pour les tests (sslip.io = pas de DNS).
# En prod (Phase 3), on basculera sur s1.yessalerp.com via ce même paramètre.
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "51.255.204.248.sslip.io")


# ---------------------------------------------------------------------------
# Catalogue de packs
# ---------------------------------------------------------------------------
# Chaque pack = un repo Git (docker-compose.yml de prod) + sa deploy key Coolify.
# Les images sont sur ghcr.io (privées) ; l'hôte de déploiement est authentifié
# via `docker login ghcr.io`. Le service applicatif s'appelle toujours 'dolib'.
#
# Catalogue en dur pour démarrer (POS validé). Tambali/Asso/Pro/Immo s'ajoutent
# ici au fur et à mesure. À terme, ce catalogue pourra être lu depuis une table
# ou depuis les releases GitHub.
#
# Les UUID de deploy key viennent du .env (un par pack) pour ne pas coder de
# secret/identifiant d'infra en dur dans le code versionné.

PACKS: dict[str, dict] = {
    "pos": {
        "label": "Pack POS (Tambali + TakePOS)",
        "repo": os.environ.get(
            "PACK_POS_REPOSITORY",
            "git@github.com:jaxaayannuaire/jgh-pack-pos.git"),
        "branch": os.environ.get("PACK_POS_BRANCH", "main"),
        "deploy_key_uuid": os.environ.get("PACK_POS_DEPLOY_KEY_UUID", ""),
        "service": "dolib",
        "version": "1.0.0",
    },
    # "tambali": { ... },
    # "asso":    { ... },
    # "pro":     { ... },
    # "immo":    { ... },
}

DEFAULT_PACK = os.environ.get("DEFAULT_PACK", "pos")


def is_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_IDS

def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS


def build_connector() -> CoolifyConnector:
    cfg = CoolifyConfig(
        base_url=os.environ["COOLIFY_BASE_URL"],
        token=os.environ["COOLIFY_TOKEN"],
        server_uuid=os.environ["COOLIFY_SERVER_UUID"],
        project_uuid=os.environ["COOLIFY_PROJECT_UUID"],
        environment_name=os.environ["COOLIFY_ENVIRONMENT_NAME"],
        environment_uuid=os.environ["COOLIFY_ENVIRONMENT_UUID"],
        timeout=int(os.environ.get("COOLIFY_TIMEOUT", "30")),
    )
    return CoolifyConnector(cfg)


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "JGH Clone Bot — provisioning Coolify.\n"
        "Commandes : /packs · /provision <nom> <pack> · /jobs · /job <id> · /version"
    )

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    conn = context.bot_data["coolify"]
    try:
        h = conn.healthcheck()
        etat = "✅ joignable" if h["api_reachable"] else "❌ injoignable"
        await update.message.reply_text(
            f"JGH Clone Bot v0.2 (packs)\nCoolify : {etat}\n"
            f"Serveur usable : {h['server_usable']}\n"
            f"Packs au catalogue : {len(PACKS)}")
    except CoolifyError as e:
        await update.message.reply_text(f"JGH Clone Bot v0.2\nCoolify : ❌ {e}")


async def cmd_provision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/provision <nom> [pack] [domaine] — prépare un déploiement (dry-run + confirmation).

    - nom    : identifiant du client (ex: boutiquekhadim)
    - pack   : clé du catalogue (défaut: DEFAULT_PACK). Ex: pos
    - domaine: optionnel ; sinon dérivé du nom sur DOMAIN_SUFFIX
    """
    if not is_allowed(update):
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage : /provision <nom> [pack] [domaine]\n"
            "Ex : /provision boutiquekhadim pos\n"
            "     /provision boutiquekhadim pos boutiquekhadim.s1.yessalerp.com\n\n"
            "Packs disponibles : /packs")
        return

    name = args[0].strip().lower()

    # Résolution du pack : 2e argument s'il correspond à une clé du catalogue,
    # sinon on prend le pack par défaut (et le 2e arg est alors le domaine).
    pack_key = DEFAULT_PACK
    domain_arg_index = 1
    if len(args) > 1 and args[1].strip().lower() in PACKS:
        pack_key = args[1].strip().lower()
        domain_arg_index = 2

    pack = PACKS.get(pack_key)
    if not pack:
        await update.message.reply_text(
            f"⚠️ Pack inconnu : `{pack_key}`. Voir /packs.",
            parse_mode="Markdown")
        return

    if not pack["deploy_key_uuid"]:
        await update.message.reply_text(
            f"⚠️ Deploy key manquante pour le pack `{pack_key}` "
            f"(variable .env absente) — déploiement impossible.",
            parse_mode="Markdown")
        return

    # Nom d'app Coolify : préfixe + pack + horodatage (évite les collisions)
    app_name = f"jgh-{name}-{pack_key}-{int(time.time())}"
    # Domaine : fourni en argument, ou dérivé du nom sur le suffixe
    domain = (args[domain_arg_index].strip()
              if len(args) > domain_arg_index
              else f"{name}.{DOMAIN_SUFFIX}")

    db: Database = context.bot_data["db"]

    # Idempotence : un même (nom, pack, domaine) ne relance pas un doublon
    idem = f"provision:{name}:{pack_key}:{domain}"
    existing = db.job_exists_for_key(idem)
    if existing:
        await update.message.reply_text(
            f"⚠️ Un job existe déjà pour ce nom/pack/domaine (job #{existing}).\n"
            f"Voir /job {existing}. Change le nom pour un nouveau déploiement.")
        return

    # Création du job en dry-run + file de confirmation
    job_id = db.create_job(
        client_name=name, subdomain=domain,
        git_repository=pack["repo"], git_branch=pack["branch"],
        idempotency_key=idem)

    plan = (
        f"📋 *Plan de déploiement* (dry-run)\n\n"
        f"Job #{job_id}\n"
        f"Client : `{name}`\n"
        f"Pack : `{pack_key}` — {pack['label']} v{pack['version']}\n"
        f"Nom app : `{app_name}`\n"
        f"Domaine : `{domain}`\n"
        f"Repo : `{pack['repo']}`\n"
        f"Service : `{pack['service']}`\n\n"
        f"Confirme pour lancer le déploiement Coolify."
    )
    pending_id = db.create_pending(job_id, "provision", plan)

    # Mémoriser les paramètres de déploiement liés à cette confirmation.
    # (app_name + pack, pour que le callback sache quoi déployer)
    context.bot_data.setdefault("deploy_ctx", {})[pending_id] = {
        "app_name": app_name,
        "pack_key": pack_key,
        "deploy_key_uuid": pack["deploy_key_uuid"],
        "service": pack["service"],
    }

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmer", callback_data=f"ok:{pending_id}"),
        InlineKeyboardButton("❌ Ignorer", callback_data=f"no:{pending_id}"),
    ]])
    await update.message.reply_text(plan, reply_markup=kb, parse_mode="Markdown")


async def cmd_packs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/packs — liste le catalogue de packs déployables."""
    if not is_allowed(update):
        return
    if not PACKS:
        await update.message.reply_text("Aucun pack au catalogue.")
        return
    lines = ["*Catalogue de packs*"]
    for key, p in PACKS.items():
        ready = "✅" if p["deploy_key_uuid"] else "⚠️ (deploy key manquante)"
        lines.append(f"• `{key}` — {p['label']} v{p['version']} {ready}")
    lines.append("\nDéployer : /provision <nom> <pack>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback des boutons ✅/❌."""
    query = update.callback_query
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Réservé aux admins.")
        return

    action, pid_str = query.data.split(":", 1)
    pending_id = int(pid_str)
    db: Database = context.bot_data["db"]
    pending = db.get_pending(pending_id)

    if not pending or pending["status"] != "pending":
        await query.edit_message_text("⚠️ Cette action n'est plus en attente.")
        return

    job_id = pending["job_id"]

    if action == "no":
        db.resolve_pending(pending_id, "rejected")
        db.set_job_status(job_id, "failed",
                          error="Annulé par l'admin", resolved=True)
        await query.edit_message_text(f"❌ Déploiement annulé (job #{job_id}).")
        return

    # action == "ok" : on exécute
    db.resolve_pending(pending_id, "confirmed")
    db.set_job_status(job_id, "confirmed")

    dctx = context.bot_data.get("deploy_ctx", {}).get(pending_id, {})
    app_name = dctx.get("app_name", f"jgh-job{job_id}")
    deploy_key_uuid = dctx.get("deploy_key_uuid", "")
    service = dctx.get("service", "dolib")
    job = db.get_job(job_id)

    if not deploy_key_uuid:
        db.set_job_status(job_id, "failed",
                          error="Deploy key absente au moment de la confirmation",
                          resolved=True)
        await query.edit_message_text(
            f"❌ Deploy key introuvable (job #{job_id}). Réessaie /provision.")
        return

    await query.edit_message_text(
        f"⏳ Déploiement en cours (job #{job_id})…\n"
        f"Création de l'application Coolify.")

    conn: CoolifyConnector = context.bot_data["coolify"]

    # 1. Création
    try:
        db.set_job_status(job_id, "running", append_log="create_compose_application")
        resp = conn.create_compose_application(
            name=app_name,
            git_repository=job["git_repository"],
            git_branch=job["git_branch"],
            private_key_uuid=deploy_key_uuid,
            domain=job["subdomain"],
            compose_service_name=service,
            instant_deploy=False,
            force_domain_override=False,
        )
    except CoolifyDomainConflict as e:
        db.set_job_status(job_id, "failed", error=str(e), resolved=True)
        await query.edit_message_text(f"❌ Domaine déjà pris (job #{job_id}).")
        return
    except (CoolifyAuthError, CoolifyNotFound, CoolifyError) as e:
        db.set_job_status(job_id, "failed", error=str(e),
                          append_log=f"ERREUR create: {e}", resolved=True)
        await query.edit_message_text(f"❌ Échec création (job #{job_id}) : {e}")
        return

    app_uuid = resp.get("uuid")
    if not app_uuid:
        db.set_job_status(job_id, "failed",
                          error="Pas d'UUID dans la réponse Coolify",
                          append_log=f"réponse: {str(resp)[:400]}", resolved=True)
        await query.edit_message_text(
            f"❌ Création sans UUID (job #{job_id}). Voir logs.")
        return

    db.set_job_status(job_id, "running", app_uuid=app_uuid,
                      append_log=f"app créée: {app_uuid}")

    # 2. Déploiement
    try:
        conn.deploy(app_uuid)
        db.set_job_status(job_id, "running", append_log="deploy déclenché")
    except CoolifyError as e:
        db.set_job_status(job_id, "failed", error=str(e),
                          append_log=f"ERREUR deploy: {e}", resolved=True)
        await query.edit_message_text(
            f"❌ App créée mais déploiement échoué (job #{job_id}) : {e}")
        return

    # 3. Statut actif (le déploiement continue en arrière-plan côté Coolify)
    db.set_job_status(job_id, "active", resolved=True,
                      append_log="statut actif")
    await query.edit_message_text(
        f"✅ *Déploiement lancé* (job #{job_id})\n\n"
        f"App : `{app_name}`\n"
        f"URL : https://{job['subdomain']}/\n\n"
        f"Le premier déploiement prend 2–6 min (téléchargement des images).\n"
        f"Suivi : /job {job_id}",
        parse_mode="Markdown")


async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    db: Database = context.bot_data["db"]
    jobs = db.recent_jobs(10)
    if not jobs:
        await update.message.reply_text("Aucun job pour l'instant.")
        return
    lines = ["*Jobs récents*"]
    icons = {"active": "✅", "failed": "❌", "running": "⏳",
             "pending": "🕓", "confirmed": "⏳"}
    for j in jobs:
        ic = icons.get(j["status"], "•")
        lines.append(f"{ic} #{j['id']} `{j['subdomain']}` — {j['status']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage : /job <id>")
        return
    db: Database = context.bot_data["db"]
    job = db.get_job(int(context.args[0]))
    if not job:
        await update.message.reply_text("Job introuvable.")
        return
    txt = (
        f"*Job #{job['id']}* — {job['status']}\n"
        f"Nom : `{job['client_name']}`\n"
        f"Domaine : `{job['subdomain']}`\n"
        f"App UUID : `{job['coolify_app_uuid'] or '—'}`\n"
    )
    if job["error_message"]:
        txt += f"Erreur : {job['error_message']}\n"
    if job["stdout_log"]:
        txt += f"\nLog :\n```\n{job['stdout_log'][-600:]}\n```"
    await update.message.reply_text(txt, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Démarrage
# ---------------------------------------------------------------------------

def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    db = Database(os.environ.get("DB_PATH", "clone.duckdb"))
    coolify = build_connector()

    app = Application.builder().token(token).build()
    app.bot_data["db"] = db
    app.bot_data["coolify"] = coolify

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("packs", cmd_packs))
    app.add_handler(CommandHandler("provision", cmd_provision))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^(ok|no):"))

    logger.info("JGH Clone Bot démarré (allowed=%d, admins=%d)",
                len(ALLOWED_IDS), len(ADMIN_IDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
