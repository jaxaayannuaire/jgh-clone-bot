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

from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    filters, ContextTypes,
)

from db.duckdb_client import Database
from coolify_connector import (
    CoolifyConnector, CoolifyConfig,
    CoolifyError, CoolifyAuthError, CoolifyDomainConflict, CoolifyNotFound,
)
from woo_connector import (
    WooConnector, WooConfig, WooError, WooAuthError, WooNotFound,
    PRODUCT_TO_PACK,
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

def _int_or_none(val: str) -> Optional[int]:
    """Convertit une chaîne .env en int, ou None si vide/invalide."""
    val = (val or "").strip()
    if not val:
        return None
    try:
        return int(val)
    except ValueError:
        return None


PACKS: dict[str, dict] = {
    "pos": {
        "label": "Pack POS (Tambali + TakePOS)",
        "repo": os.environ.get(
            "PACK_POS_REPOSITORY",
            "git@github.com:jaxaayannuaire/jgh-pack-pos.git"),
        "branch": os.environ.get("PACK_POS_BRANCH", "main"),
        "deploy_key_uuid": os.environ.get("PACK_POS_DEPLOY_KEY_UUID", ""),
        "product_id": _int_or_none(os.environ.get("PACK_POS_PRODUCT_ID", "")),
        "service": "dolib",
        "version": "1.0.0",
    },
    "tambali": {
        "label": "Pack Tambali",
        "repo": os.environ.get("PACK_TAMBALI_REPOSITORY",
                               "git@github.com:jaxaayannuaire/jgh-pack-tambali.git"),
        "branch": os.environ.get("PACK_TAMBALI_BRANCH", "main"),
        "deploy_key_uuid": os.environ.get("PACK_TAMBALI_DEPLOY_KEY_UUID", ""),
        "product_id": _int_or_none(os.environ.get("PACK_TAMBALI_PRODUCT_ID", "")),
        "service": "dolib",
        "version": "1.0.0",
    },
    "asso": {
        "label": "Pack Asso",
        "repo": os.environ.get("PACK_ASSO_REPOSITORY",
                               "git@github.com:jaxaayannuaire/jgh-pack-asso.git"),
        "branch": os.environ.get("PACK_ASSO_BRANCH", "main"),
        "deploy_key_uuid": os.environ.get("PACK_ASSO_DEPLOY_KEY_UUID", ""),
        "product_id": _int_or_none(os.environ.get("PACK_ASSO_PRODUCT_ID", "")),
        "service": "dolib",
        "version": "1.0.0",
    },
    "pro": {
        "label": "Pack Pro",
        "repo": os.environ.get("PACK_PRO_REPOSITORY",
                               "git@github.com:jaxaayannuaire/jgh-pack-pro.git"),
        "branch": os.environ.get("PACK_PRO_BRANCH", "main"),
        "deploy_key_uuid": os.environ.get("PACK_PRO_DEPLOY_KEY_UUID", ""),
        "product_id": _int_or_none(os.environ.get("PACK_PRO_PRODUCT_ID", "")),
        "service": "dolib",
        "version": "1.0.0",
    },
}

DEFAULT_PACK = os.environ.get("DEFAULT_PACK", "pos")


def pack_is_deployable(pack_key: str) -> bool:
    """Un pack est déployable s'il a une deploy key configurée."""
    p = PACKS.get(pack_key)
    return bool(p and p.get("deploy_key_uuid"))


def build_product_mapping() -> dict[int, str]:
    """Construit le mapping product_id WooCommerce -> clé de pack depuis le
    catalogue (product_id lu du .env). Ignore les packs sans product_id."""
    mapping: dict[int, str] = {}
    for key, p in PACKS.items():
        pid = p.get("product_id")
        if pid:
            mapping[pid] = key
    return mapping

# --- Suivi de déploiement (notification de fin) ---
# Intervalle entre deux vérifications de l'état du déploiement.
POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "15"))
# Délai maximal de suivi ; au-delà, on prévient sans conclure (⚠️).
POLL_TIMEOUT_S = int(os.environ.get("POLL_TIMEOUT_S", str(12 * 60)))
# Après la fin du déploiement Coolify, le conteneur peut encore démarrer
# (MariaDB importe le dump, Dolibarr boote). On accorde un délai de grâce :
# GRACE_MAX_ATTEMPTS vérifications espacées de POLL_INTERVAL_S avant de
# conclure à l'échec. 8 × 15 s = 2 min de grâce (le 1er déploiement d'un pack
# dure ~1min30, les suivants ~30 s).
GRACE_MAX_ATTEMPTS = int(os.environ.get("GRACE_MAX_ATTEMPTS", "8"))
# Version de Dolibarr des packs (affichée dans le message de fin).
DOLIBARR_VERSION = os.environ.get("DOLIBARR_VERSION", "22.0.4")


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


def build_woo_connector() -> Optional[WooConnector]:
    """Construit le connecteur WooCommerce si les clés sont configurées.

    Renvoie None si non configuré (le bot fonctionne alors sans /commandes).
    Les valeurs sont nettoyées (.strip()) car un espace/retour parasite dans
    le .env suffit à provoquer un 401 à l'authentification.
    """
    key = os.environ.get("WOO_CONSUMER_KEY", "").strip()
    secret = os.environ.get("WOO_CONSUMER_SECRET", "").strip()
    base = os.environ.get("WOO_BASE_URL", "").strip()
    if not (key and secret and base):
        return None
    cfg = WooConfig(
        base_url=base, consumer_key=key, consumer_secret=secret,
        timeout=int(os.environ.get("WOO_TIMEOUT", "30")),
    )
    return WooConnector(cfg, product_mapping=build_product_mapping())


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "JGH Clone Bot — provisioning Coolify.\n"
        "Commandes : /packs · /commandes · /provision <nom> <pack> [test] · "
        "/instances · /delete <id> · /jobs · /job <id> · /version"
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

    # Le mot-clé 'test' (n'importe où après le nom) marque une instance de test.
    # On le retire des args avant de résoudre pack/domaine.
    raw_rest = [a.strip() for a in args[1:]]
    instance_type = "client"
    if "test" in [a.lower() for a in raw_rest]:
        instance_type = "test"
        raw_rest = [a for a in raw_rest if a.lower() != "test"]

    # Résolution du pack : 1er reste s'il correspond à une clé du catalogue,
    # sinon on prend le pack par défaut (et le reste est alors le domaine).
    pack_key = DEFAULT_PACK
    domain_val = None
    if raw_rest and raw_rest[0].lower() in PACKS:
        pack_key = raw_rest[0].lower()
        domain_val = raw_rest[1] if len(raw_rest) > 1 else None
    elif raw_rest:
        domain_val = raw_rest[0]

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
    domain = domain_val.strip() if domain_val else f"{name}.{DOMAIN_SUFFIX}"

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
        idempotency_key=idem, instance_type=instance_type)

    type_badge = "🧪 TEST" if instance_type == "test" else "👤 CLIENT"
    plan = (
        f"📋 *Plan de déploiement* (dry-run)\n\n"
        f"Job #{job_id}\n"
        f"Type : {type_badge}\n"
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

    await _launch_deployment(
        context, job_id=job_id, app_name=app_name,
        deploy_key_uuid=deploy_key_uuid, service=service,
        chat_id=query.message.chat_id, query=query)


async def _launch_deployment(context, *, job_id, app_name, deploy_key_uuid,
                             service, chat_id, query=None):
    """
    Crée l'app Coolify, déclenche le déploiement, planifie le suivi.
    Partagé par /provision (via on_confirm) et /commandes (via on_woo_provision).

    query : callback query à éditer pour les messages d'étape (optionnel ;
            si None, on envoie de nouveaux messages via chat_id).
    """
    db: Database = context.bot_data["db"]
    conn: CoolifyConnector = context.bot_data["coolify"]
    job = db.get_job(job_id)

    async def _notify(txt, md=False):
        kwargs = {"parse_mode": "Markdown"} if md else {}
        if query is not None:
            await query.edit_message_text(txt, **kwargs)
        else:
            await context.bot.send_message(chat_id, txt, **kwargs)

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
        await _notify(f"❌ Domaine déjà pris (job #{job_id}).")
        return
    except (CoolifyAuthError, CoolifyNotFound, CoolifyError) as e:
        db.set_job_status(job_id, "failed", error=str(e),
                          append_log=f"ERREUR create: {e}", resolved=True)
        await _notify(f"❌ Échec création (job #{job_id}) : {e}")
        return

    app_uuid = resp.get("uuid")
    if not app_uuid:
        db.set_job_status(job_id, "failed",
                          error="Pas d'UUID dans la réponse Coolify",
                          append_log=f"réponse: {str(resp)[:400]}", resolved=True)
        await _notify(f"❌ Création sans UUID (job #{job_id}). Voir logs.")
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
        await _notify(f"❌ App créée mais déploiement échoué (job #{job_id}) : {e}")
        return

    # 3. Suivi en tâche de fond
    db.set_job_status(job_id, "running", append_log="suivi du déploiement lancé")
    await _notify(
        f"⏳ *Déploiement en cours* (job #{job_id})\n\n"
        f"Client : `{job['client_name']}`\n"
        f"App : `{app_name}`\n"
        f"URL : https://{job['subdomain']}/\n\n"
        f"Téléchargement des images et démarrage… "
        f"Je te préviens dès que c'est prêt.\n"
        f"Suivi manuel : /job {job_id}", md=True)

    context.job_queue.run_once(
        poll_deployment,
        when=POLL_INTERVAL_S,
        data={
            "job_id": job_id,
            "app_uuid": app_uuid,
            "app_name": app_name,
            "domain": job["subdomain"],
            "chat_id": chat_id,
            "started_at": time.time(),
            "attempts": 0,
            "grace_attempts": 0,
        },
        name=f"poll_job_{job_id}",
    )


def _human_duration(seconds: float) -> str:
    """Formate une durée en 'X min Y s' (ou 'Y s' si < 1 min)."""
    s = int(round(seconds))
    m, s = divmod(s, 60)
    return f"{m} min {s} s" if m else f"{s} s"


async def poll_deployment(context: ContextTypes.DEFAULT_TYPE):
    """
    Tâche de fond (job_queue) : suit un déploiement jusqu'à sa fin.

    Logique validée contre Coolify 4.1.2 :
      - tant que le déploiement figure dans /deployments (in_progress) → on
        replanifie une vérification dans POLL_INTERVAL_S ;
      - dès qu'il disparaît de /deployments → le déploiement est terminé ;
        on lit alors l'état de l'app : running = ✅ succès, sinon 🔴 échec ;
      - au-delà de POLL_TIMEOUT_S → ⚠️ on prévient sans conclure.

    Le message de fin s'inspire de l'e-mail d'installation OVH : statut,
    paramètres d'accès, et liens (dont /job détaillé).
    """
    data = context.job.data
    job_id = data["job_id"]
    app_uuid = data["app_uuid"]
    app_name = data["app_name"]
    domain = data["domain"]
    chat_id = data["chat_id"]
    started_at = data["started_at"]
    attempts = data["attempts"] + 1

    db: Database = context.bot_data["db"]
    conn: CoolifyConnector = context.bot_data["coolify"]

    elapsed = time.time() - started_at

    # 1. Toujours en cours ? (présent dans /deployments)
    try:
        still_active = conn.is_deployment_active(
            app_uuid=app_uuid, app_name=app_name)
    except CoolifyError as e:
        # Erreur API transitoire : on ne conclut pas, on retente au prochain tour
        logger.warning("poll job #%d : erreur API (%s), on retente", job_id, e)
        still_active = True

    if still_active:
        # 2. Dépassement du délai maximal ?
        if elapsed >= POLL_TIMEOUT_S:
            db.set_job_status(
                job_id, "running",
                append_log=f"timeout suivi après {_human_duration(elapsed)}")
            await context.bot.send_message(
                chat_id,
                f"⚠️ *Déploiement toujours en cours* (job #{job_id})\n\n"
                f"Après {_human_duration(elapsed)}, le déploiement n'est pas "
                f"terminé. Il continue peut-être côté Coolify — vérifie "
                f"manuellement.\n"
                f"URL : https://{domain}/\n"
                f"Détails : /job {job_id}",
                parse_mode="Markdown")
            return

        # Sinon on replanifie une vérification
        data["attempts"] = attempts
        context.job_queue.run_once(
            poll_deployment, when=POLL_INTERVAL_S, data=data,
            name=f"poll_job_{job_id}")
        return

    # 3. Le déploiement a disparu de /deployments → terminé côté Coolify.
    #    MAIS le conteneur peut encore démarrer (MariaDB importe le dump, puis
    #    Dolibarr boote). On accorde un DÉLAI DE GRÂCE : tant que l'app n'est pas
    #    'running', on retente quelques fois avant de conclure à l'échec.
    running = conn.application_is_running(app_uuid)
    duree = _human_duration(elapsed)

    if running:
        db.set_job_status(
            job_id, "active", resolved=True,
            append_log=f"déploiement réussi en {duree}")
        await context.bot.send_message(
            chat_id,
            f"✅ *Instance déployée avec succès !* (job #{job_id})\n\n"
            f"🌐 URL : https://{domain}/\n"
            f"🐘 Dolibarr : {DOLIBARR_VERSION}\n"
            f"⏱️ Déployée en {duree}\n\n"
            f"🔗 Détails : /job {job_id}",
            parse_mode="Markdown")
        return

    # Pas encore 'running' : phase de grâce (le conteneur démarre peut-être).
    grace_attempts = data.get("grace_attempts", 0) + 1
    if grace_attempts <= GRACE_MAX_ATTEMPTS:
        logger.info(
            "poll job #%d : déploiement fini mais app pas encore running, "
            "grâce %d/%d", job_id, grace_attempts, GRACE_MAX_ATTEMPTS)
        db.set_job_status(
            job_id, "running",
            append_log=f"attente démarrage conteneur (grâce {grace_attempts}"
                       f"/{GRACE_MAX_ATTEMPTS})")
        data["attempts"] = attempts
        data["grace_attempts"] = grace_attempts
        context.job_queue.run_once(
            poll_deployment, when=POLL_INTERVAL_S, data=data,
            name=f"poll_job_{job_id}")
        return

    # Délai de grâce épuisé : là on conclut vraiment à l'échec.
    db.set_job_status(
        job_id, "failed",
        error="L'application n'est pas 'running' après le déploiement "
              "et le délai de grâce",
        append_log=f"échec constaté après {duree} (grâce épuisée)", resolved=True)
    await context.bot.send_message(
        chat_id,
        f"🔴 *Déploiement bloqué* (job #{job_id})\n\n"
        f"L'application n'a pas démarré correctement après {duree}.\n"
        f"URL prévue : https://{domain}/\n\n"
        f"🔗 Diagnostic : /job {job_id}\n"
        f"Vérifie les logs du déploiement dans Coolify.",
        parse_mode="Markdown")


async def cmd_instances(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/instances — liste les instances déployées avec type, statut, mise en ligne."""
    if not is_allowed(update):
        return
    db: Database = context.bot_data["db"]
    instances = db.list_instances(30)
    if not instances:
        await update.message.reply_text("Aucune instance déployée.")
        return

    status_icons = {"active": "🟢 en ligne", "running": "⏳ en cours",
                    "failed": "🔴 échec", "deleted": "🗑️ supprimée"}
    lines = ["*Instances déployées*\n"]
    for it in instances:
        type_badge = "🧪" if it["instance_type"] == "test" else "👤"
        etat = status_icons.get(it["status"], it["status"])
        # Date de mise en ligne (online_at), sinon création
        when = it["online_at"] or it["created_at"]
        when_str = str(when)[:16] if when else "—"
        lines.append(
            f"{type_badge} #{it['id']} `{it['client_name']}` — {etat}\n"
            f"   `{it['subdomain']}`\n"
            f"   en ligne : {when_str}")
    lines.append("\nSupprimer : /delete <id>")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/delete <id> — supprime une instance (résiliation, données comprises).

    Instance TEST   → double confirmation par boutons.
    Instance CLIENT → saisie du nom exact pour confirmer (façon Coolify).
    """
    if not is_allowed(update):
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage : /delete <id>\n"
            "L'id est celui d'un job/instance (voir /instances ou /jobs).")
        return

    try:
        job_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("L'id doit être un nombre. Ex : /delete 6")
        return

    db: Database = context.bot_data["db"]
    job = db.get_job(job_id)
    if not job:
        await update.message.reply_text(f"Job #{job_id} introuvable.")
        return

    # Garde-fous
    if not job.get("coolify_app_uuid"):
        await update.message.reply_text(
            f"⚠️ Le job #{job_id} n'a pas d'application Coolify associée "
            f"(rien à supprimer côté infra).")
        return
    if job["status"] == "deleted":
        await update.message.reply_text(
            f"⚠️ L'instance #{job_id} est déjà supprimée.")
        return
    if job["status"] in ("running", "confirmed", "pending"):
        await update.message.reply_text(
            f"⚠️ Le déploiement #{job_id} est encore en cours (statut "
            f"`{job['status']}`). Attends la fin avant de supprimer.",
            parse_mode="Markdown")
        return

    itype = job.get("instance_type", "client")
    app_uuid = job["coolify_app_uuid"]

    if itype == "test":
        # --- Flux TEST : double confirmation par boutons ---
        pending_id = db.create_pending(job_id, "delete",
                                       f"delete test #{job_id}")
        context.bot_data.setdefault("delete_ctx", {})[pending_id] = {
            "job_id": job_id, "app_uuid": app_uuid,
            "name": job["client_name"], "domain": job["subdomain"],
        }
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑️ Supprimer", callback_data=f"del1:{pending_id}"),
            InlineKeyboardButton("❌ Annuler", callback_data=f"delno:{pending_id}"),
        ]])
        await update.message.reply_text(
            f"🧪 *Suppression d'une instance de TEST*\n\n"
            f"Job #{job_id} — `{job['client_name']}`\n"
            f"Domaine : `{job['subdomain']}`\n\n"
            f"⚠️ L'application et ses *données* seront supprimées "
            f"définitivement.\n"
            f"Confirmer ?",
            reply_markup=kb, parse_mode="Markdown")
    else:
        # --- Flux CLIENT : saisie du nom exact (façon Coolify) ---
        # On mémorise l'attente d'une saisie de confirmation pour cet admin.
        uid = update.effective_user.id
        context.bot_data.setdefault("delete_await", {})[uid] = {
            "job_id": job_id, "app_uuid": app_uuid,
            "name": job["client_name"], "domain": job["subdomain"],
        }
        await update.message.reply_text(
            f"👤 *Suppression d'une instance CLIENT*\n\n"
            f"Job #{job_id} — `{job['client_name']}`\n"
            f"Domaine : `{job['subdomain']}`\n\n"
            f"⚠️ *Action irréversible* : l'application et toutes les "
            f"*données du client* seront définitivement perdues.\n\n"
            f"Pour confirmer, envoie exactement le nom de l'instance :\n"
            f"`{job['client_name']}`\n\n"
            f"Ou /cancel pour annuler.",
            parse_mode="Markdown")


async def on_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callbacks de la suppression d'instance TEST (boutons)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("⛔ Réservé aux admins.")
        return

    action, pid_str = query.data.split(":", 1)
    pending_id = int(pid_str)
    db: Database = context.bot_data["db"]
    dctx = context.bot_data.get("delete_ctx", {}).get(pending_id)

    if not dctx:
        await query.edit_message_text("⚠️ Cette demande n'est plus valide.")
        return

    if action == "delno":
        db.resolve_pending(pending_id, "rejected")
        context.bot_data["delete_ctx"].pop(pending_id, None)
        await query.edit_message_text(
            f"❌ Suppression annulée (job #{dctx['job_id']}).")
        return

    # action == "del1" : premier bouton cliqué → SECONDE confirmation
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Oui, supprimer définitivement",
                             callback_data=f"del2:{pending_id}"),
        InlineKeyboardButton("❌ Non", callback_data=f"delno:{pending_id}"),
    ]])
    await query.edit_message_text(
        f"⚠️ *Confirmation finale*\n\n"
        f"Supprimer définitivement l'instance de test "
        f"`{dctx['name']}` (job #{dctx['job_id']}) et ses données ?\n\n"
        f"Cette action est *irréversible*.",
        reply_markup=kb, parse_mode="Markdown")


async def on_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback del2 : exécution effective de la suppression (test)."""
    query = update.callback_query
    await query.answer()
    if not is_admin(update):
        await query.edit_message_text("⛔ Réservé aux admins.")
        return

    _, pid_str = query.data.split(":", 1)
    pending_id = int(pid_str)
    db: Database = context.bot_data["db"]
    dctx = context.bot_data.get("delete_ctx", {}).get(pending_id)
    if not dctx:
        await query.edit_message_text("⚠️ Cette demande n'est plus valide.")
        return

    db.resolve_pending(pending_id, "confirmed")
    context.bot_data["delete_ctx"].pop(pending_id, None)
    await _do_delete(update, context, dctx, edit=True)


async def on_delete_name_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """MessageHandler : capture la saisie du nom pour les suppressions CLIENT."""
    if not is_admin(update):
        return
    uid = update.effective_user.id
    awaiting = context.bot_data.get("delete_await", {}).get(uid)
    if not awaiting:
        return  # Pas de suppression client en attente pour cet utilisateur

    text = (update.message.text or "").strip()

    if text.lower() == "/cancel":
        context.bot_data["delete_await"].pop(uid, None)
        await update.message.reply_text(
            f"❌ Suppression annulée (job #{awaiting['job_id']}).")
        return

    if text != awaiting["name"]:
        await update.message.reply_text(
            f"⚠️ Le nom ne correspond pas. Suppression *non* effectuée.\n"
            f"Attendu : `{awaiting['name']}`\n"
            f"Réessaie /delete {awaiting['job_id']} ou /cancel.",
            parse_mode="Markdown")
        context.bot_data["delete_await"].pop(uid, None)
        return

    # Nom correct → exécution
    context.bot_data["delete_await"].pop(uid, None)
    await _do_delete(update, context, awaiting, edit=False)


async def _do_delete(update, context, dctx: dict, edit: bool):
    """Exécute la suppression Coolify + met à jour la base + notifie."""
    db: Database = context.bot_data["db"]
    conn: CoolifyConnector = context.bot_data["coolify"]
    job_id = dctx["job_id"]
    app_uuid = dctx["app_uuid"]

    async def _say(txt):
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
        else:
            await update.message.reply_text(txt, parse_mode="Markdown")

    await _say(f"⏳ Suppression en cours (job #{job_id})…")

    try:
        conn.delete(app_uuid, delete_volumes=True)
    except CoolifyNotFound:
        # L'app n'existe déjà plus côté Coolify : on considère la suppression
        # comme effective et on met la base en cohérence.
        db.mark_deleted(job_id, append_log="app déjà absente côté Coolify")
        await _say(
            f"✅ *Instance supprimée* (job #{job_id})\n\n"
            f"L'application n'existait plus côté Coolify ; la base a été "
            f"mise à jour.")
        return
    except (CoolifyAuthError, CoolifyError) as e:
        db.set_job_status(job_id, db.get_job(job_id)["status"],
                          append_log=f"ERREUR suppression: {e}")
        await _say(f"🔴 Échec de la suppression (job #{job_id}) : {e}")
        return

    db.mark_deleted(job_id, append_log="suppression Coolify réussie (volumes inclus)")
    await _say(
        f"✅ *Suppression réussie* (job #{job_id})\n\n"
        f"Instance : `{dctx['name']}`\n"
        f"Domaine : `{dctx['domain']}`\n"
        f"L'application et ses données ont été supprimées.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — annule une saisie de confirmation de suppression en attente."""
    if not is_allowed(update):
        return
    uid = update.effective_user.id
    awaiting = context.bot_data.get("delete_await", {}).pop(uid, None)
    if awaiting:
        await update.message.reply_text(
            f"❌ Suppression annulée (job #{awaiting['job_id']}).")
    else:
        await update.message.reply_text("Rien à annuler.")


async def cmd_commandes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/commandes — liste les commandes WooCommerce 'completed' à provisionner."""
    if not is_allowed(update):
        return
    if not is_admin(update):
        await update.message.reply_text("⛔ Réservé aux admins.")
        return

    woo: Optional[WooConnector] = context.bot_data.get("woo")
    if woo is None:
        await update.message.reply_text(
            "⚠️ WooCommerce n'est pas configuré (clés API absentes du .env).")
        return

    db: Database = context.bot_data["db"]

    await update.message.reply_text("🔎 Lecture des commandes WooCommerce…")

    try:
        orders = woo.list_orders(status="completed", per_page=20)
    except WooAuthError as e:
        await update.message.reply_text(f"🔴 Auth WooCommerce refusée : {e}")
        return
    except WooError as e:
        await update.message.reply_text(f"🔴 Erreur WooCommerce : {e}")
        return

    if not orders:
        await update.message.reply_text(
            "Aucune commande *completed* à traiter.", parse_mode="Markdown")
        return

    # Séparer : à provisionner (pas encore de job) vs déjà fait
    to_do = []
    already = 0
    for o in orders:
        if db.job_for_woo_order(o.order_id):
            already += 1
        else:
            to_do.append(o)

    if not to_do:
        await update.message.reply_text(
            f"✅ Toutes les commandes completed récentes sont déjà "
            f"provisionnées ({already}).")
        return

    # Trier par numéro de commande décroissant (les plus récentes d'abord).
    # On trie sur l'entier du n° quand c'est possible, sinon sur l'order_id.
    def _order_sort_key(o):
        try:
            return int(o.number)
        except (ValueError, TypeError):
            return o.order_id or 0
    to_do.sort(key=_order_sort_key, reverse=True)

    # Afficher chaque commande à traiter avec un bouton "Déployer"
    header = (f"🛒 *{len(to_do)} commande(s) à provisionner*"
              f"{f' ({already} déjà faite(s))' if already else ''}\n")
    await update.message.reply_text(header, parse_mode="Markdown")

    context.bot_data.setdefault("woo_ctx", {})
    for o in to_do:
        sd = o.resolved_subdomain()
        deployable = o.pack_key and pack_is_deployable(o.pack_key)

        if o.pack_key:
            pack_txt = f"`{o.pack_key}`"
            if not deployable:
                pack_txt += " (pas encore déployable)"
        else:
            pack_txt = f"⚠️ produit {o.product_id} non mappé"

        card = (
            f"*Commande #{o.number}* · {o.date_label()}\n"
            f"Client : {o.client_label()}\n"
            f"Tél : `{o.phone or '—'}` · {o.email or '—'}\n"
            f"Produit : {o.product_name} → {pack_txt}\n"
            f"Sous-domaine : `{sd}`\n"
            f"Montant : {o.total} {o.currency}"
        )

        # Bouton Déployer UNIQUEMENT si le pack est mappé ET déployable
        if deployable:
            token = f"{o.order_id}"
            context.bot_data["woo_ctx"][token] = {
                "order_id": o.order_id, "number": o.number,
                "pack_key": o.pack_key, "subdomain": sd,
                "client_name": sd,  # le nom d'instance = le sous-domaine
            }
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Déployer",
                                     callback_data=f"woo:{token}"),
            ]])
            await update.message.reply_text(card, reply_markup=kb,
                                            parse_mode="Markdown")
        elif o.pack_key:
            # Pack reconnu mais pas encore déployable (deploy key absente)
            await update.message.reply_text(
                card + "\n\n⏳ Ce pack n'est pas encore prêt au déploiement.",
                parse_mode="Markdown")
        else:
            # Produit non mappé (pas un pack) — avertissement conservé
            await update.message.reply_text(
                card + "\n\n⚠️ Produit hors catalogue de packs (non déployable).",
                parse_mode="Markdown")


async def on_woo_provision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback 'woo:<order_id>' — provisionne l'instance d'une commande.

    Robuste au redémarrage : on ne dépend PAS d'un cache mémoire. Le token
    contient l'order_id ; on relit la commande depuis WooCommerce à la volée.
    """
    query = update.callback_query
    await query.answer()  # accuse réception du clic (stoppe le "chargement")
    if not is_admin(update):
        await query.answer("⛔ Réservé aux admins.", show_alert=True)
        return

    try:
        _, token = query.data.split(":", 1)
        order_id = int(token)
    except (ValueError, IndexError):
        await query.answer("Donnée de bouton invalide.", show_alert=True)
        return

    db: Database = context.bot_data["db"]
    woo: Optional[WooConnector] = context.bot_data.get("woo")

    if woo is None:
        await _safe_edit(query, "⚠️ WooCommerce n'est pas configuré.")
        return

    # Idempotence : refuser si déjà provisionnée (job actif ou réussi)
    existing = db.job_for_woo_order(order_id)
    if existing:
        await _safe_edit(
            query,
            f"⚠️ Commande #{order_id} déjà provisionnée (job #{existing}).\n"
            f"Pour redéployer, supprime d'abord l'instance : /delete {existing}")
        return

    # Relire la commande depuis WooCommerce (source de vérité, survit au restart)
    try:
        order = woo.get_order(order_id)
    except WooNotFound:
        await _safe_edit(query, f"⚠️ Commande #{order_id} introuvable côté WooCommerce.")
        return
    except (WooAuthError, WooError) as e:
        await _safe_edit(query, f"🔴 Erreur WooCommerce : {e}")
        return

    # Vérifier que le pack est déployable
    if not order.pack_key:
        await _safe_edit(
            query,
            f"⚠️ Commande #{order_id} : produit {order.product_id} non mappé "
            f"à un pack. Déploiement impossible.")
        return

    pack = PACKS.get(order.pack_key)
    if not pack or not pack.get("deploy_key_uuid"):
        await _safe_edit(
            query,
            f"⚠️ Pack `{order.pack_key}` pas encore déployable "
            f"(deploy key manquante).")
        return

    # Construire les paramètres de déploiement
    subdomain = order.resolved_subdomain()
    name = subdomain
    domain = f"{subdomain}.{DOMAIN_SUFFIX}"
    app_name = f"jgh-{name}-{order.pack_key}-{int(time.time())}"

    # Créer le job lié à la commande WooCommerce (instance_type=client)
    idem = f"woo:{order_id}"
    job_id = db.create_job(
        client_name=name, subdomain=domain,
        git_repository=pack["repo"], git_branch=pack["branch"],
        idempotency_key=idem, instance_type="client",
        woo_order_id=order_id)

    # Nettoyer le cache mémoire si présent (best effort)
    context.bot_data.get("woo_ctx", {}).pop(token, None)

    await _safe_edit(
        query,
        f"⏳ Déploiement de la commande #{order.number} (job #{job_id})…\n"
        f"Pack `{order.pack_key}` · `{domain}`")

    # Réutiliser la logique de déploiement + suivi
    await _launch_deployment(
        context, job_id=job_id, app_name=app_name,
        deploy_key_uuid=pack["deploy_key_uuid"],
        service=pack["service"], chat_id=query.message.chat_id)


async def _safe_edit(query, text: str):
    """Édite le message d'un callback, avec repli si l'édition échoue.

    edit_message_text peut échouer (message trop vieux, déjà modifié,
    identique). Dans ce cas on envoie un nouveau message pour que l'admin
    voie toujours le retour.
    """
    try:
        await query.edit_message_text(text, parse_mode="Markdown")
    except Exception:
        try:
            await query.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            logger.warning("Impossible d'afficher le retour du callback : %s", e)


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
    woo = build_woo_connector()

    app = Application.builder().token(token).build()
    app.bot_data["db"] = db
    app.bot_data["coolify"] = coolify
    app.bot_data["woo"] = woo

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("packs", cmd_packs))
    app.add_handler(CommandHandler("provision", cmd_provision))
    app.add_handler(CommandHandler("commandes", cmd_commandes))
    app.add_handler(CommandHandler("instances", cmd_instances))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("job", cmd_job))
    # Callbacks : provision (ok/no), suppression test (del1/delno, del2)
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^(ok|no):"))
    app.add_handler(CallbackQueryHandler(on_delete_confirm, pattern=r"^(del1|delno):"))
    app.add_handler(CallbackQueryHandler(on_delete_execute, pattern=r"^del2:"))
    app.add_handler(CallbackQueryHandler(on_woo_provision, pattern=r"^woo:"))
    # Saisie du nom pour la suppression CLIENT (texte hors commande)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, on_delete_name_reply))

    # Garde-fou : le suivi de déploiement repose sur job_queue, qui n'existe
    # que si python-telegram-bot est installé avec l'extra [job-queue].
    if app.job_queue is None:
        logger.error(
            "job_queue indisponible — installe 'python-telegram-bot[job-queue]'. "
            "La notification de fin de déploiement ne fonctionnera pas.")
    else:
        logger.info("job_queue actif : notification de fin de déploiement OK.")

    logger.info("JGH Clone Bot démarré (allowed=%d, admins=%d, packs=%d)",
                len(ALLOWED_IDS), len(ADMIN_IDS), len(PACKS))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
