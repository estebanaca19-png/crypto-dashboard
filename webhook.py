#!/usr/bin/env python3
"""
Auto-deploy webhook — escucha pushes de GitHub y actualiza el servidor automáticamente.
Corre en puerto 9000.
"""
from flask import Flask, request, jsonify
import subprocess, hmac, hashlib, os, logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "cruztrd_webhook_2026")
BOT_DIR        = "/opt/cruztrd"
GITHUB_RAW     = "https://raw.githubusercontent.com/estebanaca19-png/crypto-dashboard/main/server.py"

def verify_signature(payload, signature):
    """Verifica que el webhook viene de GitHub."""
    if not signature:
        return False
    mac = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, signature)

@app.route("/deploy", methods=["POST"])
def deploy():
    # Verificar firma de GitHub
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_signature(request.data, signature):
        logger.warning("Firma inválida — webhook rechazado")
        return jsonify({"ok": False, "msg": "Unauthorized"}), 401

    data   = request.get_json(silent=True) or {}
    branch = data.get("ref", "")

    # Solo deployar cuando se pushea a main
    if "main" not in branch:
        return jsonify({"ok": True, "msg": f"Rama {branch} ignorada"})

    logger.info("Push a main detectado — iniciando deploy...")

    try:
        # Descargar nuevo server.py
        result = subprocess.run(
            ["curl", "-o", f"{BOT_DIR}/server.py", GITHUB_RAW],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise Exception(f"Error descargando: {result.stderr}")

        # Reiniciar el servicio
        result = subprocess.run(
            ["systemctl", "restart", "cruztrd"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise Exception(f"Error reiniciando: {result.stderr}")

        logger.info("Deploy exitoso!")
        return jsonify({"ok": True, "msg": "Deploy exitoso"})

    except Exception as e:
        logger.error(f"Error en deploy: {e}")
        return jsonify({"ok": False, "msg": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9000)
