"""
diag_create.py — Diagnostic brut de POST applications/private-deploy-key.

Affiche le CORPS COMPLET de la réponse (sans filtrage), pour voir exactement
quels champs la 4.1.2 valide/refuse. Aucune extraction, aucune abstraction.

Usage : venv/bin/python diag_create.py
"""

import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.environ["COOLIFY_BASE_URL"].rstrip("/")
TOKEN = os.environ["COOLIFY_TOKEN"]
SERVER = os.environ["COOLIFY_SERVER_UUID"]
PROJECT = os.environ["COOLIFY_PROJECT_UUID"]
ENV_NAME = os.environ["COOLIFY_ENVIRONMENT_NAME"]
ENV_UUID = os.environ["COOLIFY_ENVIRONMENT_UUID"]

url = f"{BASE}/api/v1/applications/private-deploy-key"
headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

name = f"jgh-diag-{int(time.time())}"
payload = {
    "project_uuid": PROJECT,
    "server_uuid": SERVER,
    "environment_name": ENV_NAME,
    "environment_uuid": ENV_UUID,
    "name": name,
    "git_repository": "git@github.com:jaxaayannuaire/jgh-pack-pos.git",
    "git_branch": "main",
    "private_key_uuid": "PLACEHOLDER-DEPLOY-KEY-UUID",
    "build_pack": "dockercompose",
    "docker_compose_location": "/docker-compose.yml",
    "docker_compose_domains": [
        {"name": "dolib", "domain": f"https://{name}.sslip.io"}
    ],
    "instant_deploy": False,
    "force_domain_override": False,
}

print("=== Requête ===")
print("POST", url)
print(json.dumps(payload, indent=2))

resp = requests.post(url, json=payload, headers=headers, timeout=30)

print("\n=== Réponse ===")
print("HTTP", resp.status_code)
print("--- Corps brut ---")
try:
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
except ValueError:
    print(resp.text)
