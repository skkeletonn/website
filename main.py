import os
import time
import logging
import threading
import requests
from flask import Flask, request, jsonify
from config import DISCORD_TOKEN, DISCORD_KEY_API_SECRET
from key_store import get_key, delete_key, lock_hwid, GUILD_ID
import obf_access
from discord_bot import start_bot
from stickied_message_bot import start_stickied_bot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SELF_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://vadriftzbots.onrender.com")

def keep_alive_loop():
    time.sleep(30)
    while True:
        try:
            resp = requests.get(f"{SELF_URL}/health", timeout=15)
            logger.info(f"Keep-alive ping: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(600)

@app.route('/health')
def health():
    return jsonify({"status": "healthy"}), 200

@app.route('/')
def index():
    return jsonify({"status": "Bot server running"}), 200

_UNLOCK_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#1e1f22;
      color:#dbdee1;display:flex;align-items:center;justify-content:center;
      min-height:100vh;margin:0;padding:24px;box-sizing:border-box}}
 .card{{background:#2b2d31;border-radius:12px;padding:32px 28px;max-width:460px;
        text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.4)}}
 h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.6;margin:0 0 12px;font-size:15px}}
 code{{background:#1e1f22;padding:2px 6px;border-radius:4px;font-size:14px}}
 .ok{{color:#57f287}} .bad{{color:#ed4245}}
</style></head><body><div class="card">
<h1 class="{cls}">{title}</h1>{body}
</div></body></html>"""


_CLAIM_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unlock the obfuscator</title>
<style>
 body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#1e1f22;
      color:#dbdee1;display:flex;align-items:center;justify-content:center;
      min-height:100vh;margin:0;padding:24px;box-sizing:border-box}}
 .card{{background:#2b2d31;border-radius:12px;padding:32px 28px;max-width:460px;
        width:100%;text-align:center;box-shadow:0 8px 24px rgba(0,0,0,.4)}}
 h1{{font-size:20px;margin:0 0 12px}} p{{line-height:1.6;margin:0 0 14px;font-size:15px}}
 code{{background:#1e1f22;padding:2px 6px;border-radius:4px}}
 input{{width:100%;box-sizing:border-box;padding:14px;font-size:22px;text-align:center;
        letter-spacing:6px;text-transform:uppercase;background:#1e1f22;color:#fff;
        border:2px solid #404249;border-radius:8px;margin-bottom:14px}}
 input:focus{{outline:none;border-color:#5865f2}}
 button{{width:100%;padding:13px;font-size:16px;font-weight:600;background:#5865f2;
         color:#fff;border:0;border-radius:8px;cursor:pointer}}
 button:hover{{background:#4752c4}}
 .ok{{color:#57f287}} .bad{{color:#ed4245}} .hint{{font-size:13px;opacity:.7}}
</style></head><body><div class="card">{body}</div></body></html>"""

_CLAIM_FORM = """<h1>Enter your unlock code</h1>
<p>The bot DM'd you a 6-character code when you asked to unlock. Paste it below.</p>
<form method="post" action="/obf/claim">
 <input name="code" maxlength="6" autocomplete="off" autofocus
        placeholder="ABC123" required>
 <button type="submit">Unlock 24 hours</button>
</form>
<p class="hint">Lost the code? DM the bot <code>.obfunlock</code> again.</p>"""


@app.route('/obf/claim', methods=['GET'])
def obf_claim_form():
    """Destination of the static LootLabs link."""
    return _CLAIM_PAGE.format(body=_CLAIM_FORM), 200


@app.route('/obf/claim', methods=['POST'])
def obf_claim_submit():
    code = (request.form.get('code') or '').strip()
    if not code:
        return _CLAIM_PAGE.format(body="<h1 class='bad'>No code entered</h1>"
                                  + _CLAIM_FORM), 400
    try:
        discord_id, _expires = obf_access.redeem_claim_code(code)
    except obf_access.AccessError as exc:
        logger.error("obf claim failed: %s", exc)
        return _CLAIM_PAGE.format(
            body=f"<h1 class='bad'>Something went wrong</h1><p>{exc}</p>"), 500

    if not discord_id:
        return _CLAIM_PAGE.format(
            body="<h1 class='bad'>That code isn't valid</h1>"
                 "<p>It was already used, it expired (codes last 2 hours), or "
                 "it was mistyped.</p>" + _CLAIM_FORM), 400

    logger.info("obf access claimed via code for discord_id=%s", discord_id)
    return _CLAIM_PAGE.format(
        body="<h1 class='ok'>Obfuscator unlocked!</h1>"
             "<p>You have <strong>24 hours</strong> of obfuscation access.</p>"
             "<p>Head back to Discord, DM the bot, attach your <code>.lua</code> "
             "file and type <code>.obf</code>.</p>"
             "<p class='hint'>You can close this tab.</p>"), 200


@app.route('/obf/unlock/<token>')
def obf_unlock(token):
    """LootLabs redirects here after the checkpoint is completed."""
    try:
        discord_id, expires_at = obf_access.redeem(token)
    except obf_access.AccessError as exc:
        logger.error("obf unlock redeem failed: %s", exc)
        return _UNLOCK_PAGE.format(
            cls="bad", title="Something went wrong",
            body=f"<p>{exc}</p><p>Ask the bot owner for help.</p>"), 500

    if not discord_id:
        return _UNLOCK_PAGE.format(
            cls="bad", title="This link is no longer valid",
            body="<p>It was already used, or it expired. Go back to Discord and "
                 "type <code>.obfunlock</code> for a fresh link.</p>"), 400

    logger.info("obf access redeemed for discord_id=%s", discord_id)
    return _UNLOCK_PAGE.format(
        cls="ok", title="Obfuscator unlocked!",
        body="<p>You have <strong>24 hours</strong> of obfuscation access.</p>"
             "<p>Head back to Discord, DM the bot, attach your <code>.lua</code> "
             "file and type <code>.obf</code>.</p>"
             "<p>You can close this tab.</p>"), 200


@app.route('/api/validate-discord-key', methods=['POST'])
def validate_discord_key():
    data = request.get_json()
    if not data:
        return jsonify({"valid": False, "message": "No data provided"})

    secret = data.get("secret", "")
    key_value = data.get("key", "")
    hwid = data.get("hwid", "")

    if secret != DISCORD_KEY_API_SECRET:
        return jsonify({"valid": False, "message": "Unauthorized"})

    if not key_value or not hwid:
        return jsonify({"valid": False, "message": "Missing key or HWID"})

    key_data = get_key(key_value)
    if not key_data:
        return jsonify({"valid": False, "message": "Invalid key"})

    if time.time() > key_data.get("expires_at", 0):
        delete_key(key_value)
        return jsonify({"valid": False, "message": "Key expired. Run /getkey in Discord."})

    discord_id = key_data.get("discord_id")

    try:
        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}"
        }
        membership_url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{discord_id}"
        resp = requests.get(membership_url, headers=headers, timeout=10)
        logger.info(f"Discord membership check for {discord_id}: status {resp.status_code}")

        if resp.status_code == 404:
            delete_key(key_value)
            return jsonify({"valid": False, "message": "You must be in the Discord server."})
        elif resp.status_code != 200:
            logger.warning(f"Discord API returned {resp.status_code}, not deleting key")
            return jsonify({"valid": False, "message": "Verification error. Try again later."})
    except Exception as e:
        logger.error(f"Discord API error: {e}")
        return jsonify({"valid": False, "message": "Verification error. Try again later."})

    if key_data.get("hwid") and key_data["hwid"] != hwid:
        return jsonify({"valid": False, "message": "Key is locked to a different device. Use /resetkey in Discord."})

    if not key_data.get("hwid"):
        lock_hwid(key_value, hwid)

    return jsonify({"valid": True, "message": "Authenticated"})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))

    def start_bots_delayed():
        time.sleep(5)
        logger.info("Starting main bot...")
        bot_thread = threading.Thread(target=start_bot, daemon=True)
        bot_thread.start()
        time.sleep(10)
        logger.info("Starting stickied message bot...")
        stickied_bot_thread = threading.Thread(target=start_stickied_bot, daemon=True)
        stickied_bot_thread.start()

    bots_thread = threading.Thread(target=start_bots_delayed, daemon=True)
    bots_thread.start()

    keep_alive_thread = threading.Thread(target=keep_alive_loop, daemon=True)
    keep_alive_thread.start()

    logger.info(f"Bot server starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
