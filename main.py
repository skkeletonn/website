import os
import logging
import json
import secrets
import time
import requests
import threading
from utils import inject_meta_tags, server_pinger
from functools import wraps
from flask import Flask, request, jsonify, send_file, redirect, send_from_directory, make_response, render_template
from datetime import datetime, timedelta
from collections import defaultdict
from urllib.parse import urlparse

from config import *
from discord_keys_db import load_discord_keys, save_discord_keys
from youtube_grabber import YouTubeChannelFinder
from image_converter import convert_image_endpoint
from image_tools import (
    pixelate_endpoint, invert_endpoint, mirror_endpoint,
    rotate_endpoint, format_endpoint, remove_bg_endpoint,
)
from scripts_data import scripts_data, process_script_data
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'templates'))
from projects_data import projects, project_categories, process_projects_data, get_spotlight_projects
from utils import inject_meta_tags
from key_system import KeySystemManager
from verification_timer import VerificationTimer
from analytics_db import log_execution as log_execution_to_db, get_analytics as get_analytics_from_db
from guild_key_system import (
    get_guild_config, save_guild_config, init_guild_config,
    create_session, get_session, update_session,
    find_session_by_ip_and_profile, get_pending_session,
    create_guild_key, validate_guild_key,
    delete_guild_keys_by_user, get_guild_key_stats,
    cleanup_expired_guild_keys, get_destination_url,
    get_script_profile, get_profile_by_secret,
    SERVER_BASE_URL, MIN_COMPLETION_SECONDS
)
from guild_renewal_system import (
    CHECKPOINT_COUNT,
    complete_renewal_checkpoint,
    format_renewal_timestamp,
    get_renewal_entitlement,
    get_renewal_session,
    get_renewal_status,
    start_renewal_checkpoint,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "1241797935100989594")

youtube_finder = YouTubeChannelFinder()
key_system = KeySystemManager()
verification_timer = VerificationTimer(min_verification_time=25)
verification_tokens = {}
checkpoint_tokens = {}
active_checkpoints = {}

API_SECRET = os.environ.get("API_SECRET")
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY")

DISCORD_KEY_API_SECRET = os.environ.get("DISCORD_KEY_API_SECRET")

feature_credits = {}

FEATURE_CONFIG = {
    "copy-art": {
        "name": "Copy Art Credits",
        "credits_per_unlock": 2,
        "icon": "🎨",
        "description": "Copy art with special brushes from other players",
        "workink_url": "https://work.ink/20yd/Vadrifts-StarvingArtists"
    }
}

usage_data = {}
copy_usage_data = {}


def sanitize_script(script):
    safe = dict(script)
    key_link = safe.get('key_link', '') or ''
    loot_link = safe.get('lootlabs_link', '') or ''
    linkvertise_link = safe.get('linkvertise_link', '') or ''
    safe['has_workink'] = 'work.ink' in key_link or safe.get('key_type') == 'work.ink'
    safe['has_lootlabs'] = bool(loot_link)
    safe['has_linkvertise'] = bool(linkvertise_link)
    safe['has_generic_key'] = bool(key_link) and not safe['has_workink']
    safe['has_key_system'] = safe['has_workink'] or safe['has_lootlabs'] or safe['has_linkvertise'] or safe['has_generic_key']
    safe.pop('key_link', None)
    safe.pop('lootlabs_link', None)
    safe.pop('linkvertise_link', None)
    return safe


def get_client_ip():
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if client_ip and ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
    return client_ip


def is_valid_referrer(referer):
    allowed_referrers = [
        'work.ink', 'www.work.ink',
        'lootdest.org', 'lootlabs.gg', 'www.lootlabs.gg',
        'linkvertise.com', 'www.linkvertise.com',
        'link-to.net', 'direct-link.net', 'linkvertise.net',
        'link-hub.net', 'link-center.net', 'up-to-down.net'
    ]
    return any(domain in referer for domain in allowed_referrers)


@app.route('/debug-keys')
def debug_keys():
    keys = load_discord_keys()
    safe_keys = {}
    for k, v in keys.items():
        safe_keys[k[:8] + "..."] = {
            "discord_id": v.get("discord_id"),
            "expires_at": v.get("expires_at"),
            "expired": time.time() > v.get("expires_at", 0),
            "hwid": v.get("hwid"),
            "username": v.get("username")
        }
    return jsonify({
        "total_keys": len(keys),
        "guild_id_configured": GUILD_ID,
        "discord_token_set": bool(DISCORD_TOKEN),
        "keys": safe_keys
    })


def verify_turnstile(token, ip):
    try:
        response = requests.post('https://challenges.cloudflare.com/turnstile/v0/siteverify', data={
            'secret': TURNSTILE_SECRET_KEY,
            'response': token,
            'remoteip': ip
        })
        result = response.json()
        return result.get('success', False)
    except Exception as e:
        logger.error(f"Turnstile verification error: {str(e)}")
        return False


def require_api_key(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if auth_header != API_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated_function


def check_discord_membership(discord_id):
    try:
        headers = {
            "Authorization": f"Bot {DISCORD_TOKEN}"
        }
        url = f"https://discord.com/api/v10/guilds/{GUILD_ID}/members/{discord_id}"
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.status_code == 200
    except:
        return False

@app.route('/debug-guild-keys')
def debug_guild_keys():
    from guild_key_system import guild_keys_collection, script_profiles_collection
    
    profiles = []
    if script_profiles_collection is not None:
        for doc in script_profiles_collection.find():
            profiles.append({
                "profile_id": doc["_id"],
                "guild_id": doc.get("guild_id"),
                "name": doc.get("name"),
                "key_type": doc.get("key_type"),
                "secret_preview": doc.get("api_secret", "")[:12] + "...",
                "enabled": doc.get("enabled"),
                "require_membership": doc.get("require_membership")
            })

    keys = []
    if guild_keys_collection is not None:
        for doc in guild_keys_collection.find():
            keys.append({
                "key_preview": doc["_id"][:8] + "...",
                "guild_id": doc.get("guild_id"),
                "profile_id": doc.get("profile_id"),
                "discord_id": doc.get("discord_id"),
                "discord_name": doc.get("discord_name"),
                "hwid": doc.get("hwid"),
                "expired": time.time() > doc.get("expires_at", 0),
                "expires_at": doc.get("expires_at")
            })

    return jsonify({
        "profiles": profiles,
        "keys": keys,
        "total_profiles": len(profiles),
        "total_keys": len(keys)
    })
    

@app.route('/api/feature-config/<feature_id>')
def get_feature_config(feature_id):
    if feature_id not in FEATURE_CONFIG:
        return jsonify({"error": "Feature not found"}), 404
    return jsonify(FEATURE_CONFIG[feature_id])


@app.route('/unlock/<feature_id>')
def feature_unlock_page(feature_id):
    if feature_id not in FEATURE_CONFIG:
        try:
            with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
                html_content = f.read()
            html_content = html_content.replace('{{PAGE_MODE}}', 'error')
            html_content = html_content.replace('{{ERROR_TITLE}}', 'Feature Not Found')
            html_content = html_content.replace('{{ERROR_MESSAGE}}', 'This feature does not exist or the link is invalid.')
            return html_content
        except FileNotFoundError:
            return jsonify({"error": "Template not found"}), 404

    try:
        with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        html_content = html_content.replace('{{PAGE_MODE}}', 'unlock')
        html_content = html_content.replace('{{ERROR_TITLE}}', '')
        html_content = html_content.replace('{{ERROR_MESSAGE}}', '')
        html_content = html_content.replace('{{SUCCESS_ICON}}', '')
        html_content = html_content.replace('{{SUCCESS_NAME}}', '')
        html_content = html_content.replace('{{CREDITS_ADDED}}', '')
        html_content = html_content.replace('{{TOTAL_CREDITS}}', '')
        return html_content
    except FileNotFoundError:
        logger.error("feature-unlock.html template not found")
        return jsonify({"error": "Template not found"}), 404


@app.route('/start-unlock/<feature_id>')
def start_feature_unlock(feature_id):
    if feature_id not in FEATURE_CONFIG:
        return jsonify({"success": False, "error": "Feature not found"})
    client_ip = get_client_ip()
    verification_timer.start_timer(client_ip)
    logger.info(f"Feature unlock timer started for IP: {client_ip}, feature: {feature_id}")
    return jsonify({"success": True})


@app.route('/complete-unlock/<feature_id>')
def complete_feature_unlock(feature_id):
    if feature_id not in FEATURE_CONFIG:
        try:
            with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
                html_content = f.read()
            html_content = html_content.replace('{{PAGE_MODE}}', 'error')
            html_content = html_content.replace('{{ERROR_TITLE}}', 'Feature Not Found')
            html_content = html_content.replace('{{ERROR_MESSAGE}}', 'This feature does not exist.')
            return html_content, 404
        except FileNotFoundError:
            return jsonify({"error": "Template not found"}), 404

    client_ip = get_client_ip()
    referer = request.headers.get('Referer', '')
    config = FEATURE_CONFIG[feature_id]
    timer_check = verification_timer.check_timer(client_ip)

    if not timer_check['valid']:
        logger.warning(f"Invalid timer for feature unlock from IP: {client_ip}")
        try:
            with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
                html_content = f.read()
            html_content = html_content.replace('{{PAGE_MODE}}', 'error')
            html_content = html_content.replace('{{ERROR_TITLE}}', 'Access Denied')
            html_content = html_content.replace('{{ERROR_MESSAGE}}', 'Please start the unlock process from the proper page first.')
            return html_content, 403
        except FileNotFoundError:
            return jsonify({"error": "Template not found"}), 404

    if not is_valid_referrer(referer):
        logger.warning(f"Invalid referer for feature unlock from IP: {client_ip}")
        try:
            with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
                html_content = f.read()
            html_content = html_content.replace('{{PAGE_MODE}}', 'error')
            html_content = html_content.replace('{{ERROR_TITLE}}', 'Invalid Access')
            html_content = html_content.replace('{{ERROR_MESSAGE}}', 'You must complete the verification task to unlock credits.')
            return html_content, 403
        except FileNotFoundError:
            return jsonify({"error": "Template not found"}), 404

    verification_timer.mark_verified(client_ip)

    if client_ip not in feature_credits:
        feature_credits[client_ip] = {}
    if feature_id not in feature_credits[client_ip]:
        feature_credits[client_ip][feature_id] = 0

    credits_to_add = config.get("credits_per_unlock", 2)
    feature_credits[client_ip][feature_id] += credits_to_add
    new_total = feature_credits[client_ip][feature_id]

    logger.info(f"Feature '{feature_id}' credits granted to IP: {client_ip}, added: {credits_to_add}, total: {new_total}")

    try:
        with open('templates/feature-unlock.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        html_content = html_content.replace('{{PAGE_MODE}}', 'success')
        html_content = html_content.replace('{{SUCCESS_ICON}}', config.get('icon', '🎉'))
        html_content = html_content.replace('{{SUCCESS_NAME}}', config.get('name', 'Feature'))
        html_content = html_content.replace('{{CREDITS_ADDED}}', str(credits_to_add))
        html_content = html_content.replace('{{TOTAL_CREDITS}}', str(new_total))
        html_content = html_content.replace('{{ERROR_TITLE}}', '')
        html_content = html_content.replace('{{ERROR_MESSAGE}}', '')
        return html_content
    except FileNotFoundError:
        return jsonify({"error": "Template not found"}), 404


@app.route('/docs/arrayfield')
def arrayfield_docs():
    try:
        return send_file('templates/arrayfield-docs.html')
    except FileNotFoundError:
        logger.error("arrayfield-docs.html template not found")
        return jsonify({"error": "Documentation page not found"}), 404


@app.route('/check-credits/<feature_id>')
def check_feature_credits(feature_id):
    client_ip = get_client_ip()
    if client_ip not in feature_credits:
        return jsonify({"credits": 0})
    credits = feature_credits[client_ip].get(feature_id, 0)
    return jsonify({"credits": credits})


@app.route('/use-credit/<feature_id>')
def use_feature_credit(feature_id):
    client_ip = get_client_ip()
    if client_ip not in feature_credits:
        return jsonify({"success": False, "remaining": 0})
    current = feature_credits[client_ip].get(feature_id, 0)
    if current <= 0:
        return jsonify({"success": False, "remaining": 0})
    feature_credits[client_ip][feature_id] = current - 1
    logger.info(f"Credit used for '{feature_id}' by IP: {client_ip}, remaining: {current - 1}")
    return jsonify({"success": True, "remaining": current - 1})


@app.route('/')
def home():
    try:
        html_content = render_template('home.html')
        return inject_meta_tags(html_content, HOME_META_TAGS)
    except Exception as e:
        logger.error(f"home.html render failed: {e}")
        return jsonify({"error": "Home page not found"}), 404


@app.route('/scripts')
def scripts_page():
    try:
        html_content = render_template('scripts.html')
        return inject_meta_tags(html_content, SCRIPTS_META_TAGS)
    except Exception as e:
        logger.error(f"scripts.html render failed: {e}")
        return jsonify({"error": "Scripts page not found"}), 404

@app.route('/projects')
def projects_page():
    try:
        processed = process_projects_data(projects)
        html_content = render_template(
            'projects.html',
            projects=processed,
            categories=project_categories,
            spotlights=get_spotlight_projects(processed),
        )
        return inject_meta_tags(html_content, PROJECTS_META_TAGS)
    except Exception as e:
        logger.error(f"projects.html render failed: {e}")
        return jsonify({"error": "Projects page not found"}), 404

@app.route('/start-verification')
def start_verification():
    client_ip = get_client_ip()
    verification_timer.start_timer(client_ip)
    logger.info(f"Verification timer started for IP: {client_ip}")
    return jsonify({"success": True, "message": "Timer started"})


@app.route('/script/<int:script_id>')
def script_detail(script_id):
    script = next((s for s in scripts_data if s['id'] == script_id), None)
    if not script:
        return jsonify({"error": "Script not found"}), 404
    try:
        html_content = render_template('script-detail.html')
        meta_tags = f'''
    <meta property="og:title" content="{script['title']} - Vadrifts">
    <meta property="og:description" content="{script['description']}">
    <meta property="og:image" content="{script['thumbnail']}">
    <meta property="og:url" content="https://vadrifts.onrender.com/script/{script['id']}">
    <meta property="og:type" content="website">
    <meta property="og:site_name" content="Vadrifts">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{script['title']} - Vadrifts">
    <meta name="twitter:description" content="{script['description']}">
    <meta name="twitter:image" content="{script['thumbnail']}">
    <meta name="theme-color" content="#9c88ff">'''
        return inject_meta_tags(html_content, meta_tags)
    except FileNotFoundError:
        logger.error("script-detail.html template not found")
        return jsonify({"error": "Script detail page not found"}), 404

@app.route('/teenytuning')
@app.route('/audio')
@app.route('/audio-editor')
@app.route('/mintwave') 
def teenytuning():
    try:
        html_content = render_template('teenytuning.html')
        return inject_meta_tags(html_content, TEENYTUNING_META_TAGS)
    except Exception as e:
        logger.error(f"teenytuning.html render failed: {e}")
        return jsonify({"error": "TeenyTuning page not found"}), 404
        
@app.route('/converter')
@app.route('/convert')
@app.route('/resize')
@app.route('/crop')
@app.route('/pixelate')
@app.route('/pixel-art')
@app.route('/pixel-grid')
def converter():
    try:
        return send_file('templates/converter.html')
    except FileNotFoundError:
        logger.error("converter.html template not found")
        return jsonify({"error": "Converter page not found"}), 404


@app.route('/check-usage', methods=['GET'])
def check_usage():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "No HWID provided"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    if hwid in usage_data:
        if usage_data[hwid]['date'] != today:
            usage_data[hwid] = {'used': 0, 'date': today}
    else:
        usage_data[hwid] = {'used': 0, 'date': today}
    return jsonify(usage_data[hwid])


@app.route('/update-usage', methods=['GET'])
def update_usage():
    hwid = request.args.get('hwid')
    used = request.args.get('used', 0)
    if not hwid:
        return jsonify({"error": "No HWID provided"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    usage_data[hwid] = {'used': int(used), 'date': today}
    return jsonify({"success": True})


@app.route('/check-copy-usage', methods=['GET'])
def check_copy_usage():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "No HWID provided"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    if hwid in copy_usage_data:
        if copy_usage_data[hwid]['date'] != today:
            copy_usage_data[hwid] = {'texture': 0, 'normal': 0, 'date': today}
    else:
        copy_usage_data[hwid] = {'texture': 0, 'normal': 0, 'date': today}
    return jsonify(copy_usage_data[hwid])


@app.route('/update-copy-usage', methods=['GET'])
def update_copy_usage():
    hwid = request.args.get('hwid')
    texture = request.args.get('texture', 0)
    normal = request.args.get('normal', 0)
    if not hwid:
        return jsonify({"error": "No HWID provided"}), 400
    today = datetime.now().strftime("%Y-%m-%d")
    copy_usage_data[hwid] = {'texture': int(texture), 'normal': int(normal), 'date': today}
    return jsonify({"success": True})


@app.route('/log-execution', methods=['GET'])
def log_execution():
    hwid = request.args.get('hwid')
    script = request.args.get('script', 'Unknown')
    if hwid:
        log_execution_to_db(hwid, script)
    return jsonify({"success": True})


@app.route('/analytics-data', methods=['GET'])
def analytics_data():
    return jsonify(get_analytics_from_db())


@app.route('/analytics-sync', methods=['POST'])
def analytics_sync():
    return jsonify({'success': True, 'message': 'Data saves in real-time via MongoDB'})


@app.route('/analytics')
def analytics_page():
    try:
        return send_file('templates/analytics.html')
    except FileNotFoundError:
        logger.error("analytics.html template not found")
        return jsonify({"error": "Analytics page not found"}), 404


@app.route('/key-system')
def key_system_page():
    try:
        return render_template('key-system.html')
    except Exception as e:
        logger.error(f"key-system.html render failed: {e}")
        return jsonify({"error": "Key system page not found"}), 404


@app.route('/checkpoint/start')
def checkpoint_start():
    client_ip = get_client_ip()
    script_id = request.args.get('script', type=int)
    provider = request.args.get('provider', '')
    if not script_id:
        return jsonify({"error": "Invalid request"}), 400
    script = next((s for s in scripts_data if s['id'] == script_id), None)
    if not script:
        return jsonify({"error": "Script not found"}), 404

    now = time.time()
    expired = [k for k, v in checkpoint_tokens.items() if now > v['expires']]
    for k in expired:
        del checkpoint_tokens[k]

    token = secrets.token_urlsafe(32)
    checkpoint_tokens[token] = {
        'ip': client_ip,
        'script_id': script_id,
        'provider': provider,
        'expires': now + 120,
        'used': False
    }
    logger.info(f"Checkpoint token created for IP: {client_ip}, script: {script_id}, provider: {provider}")
    return jsonify({"token": token})


@app.route('/checkpoint/load')
def checkpoint_load():
    token = request.args.get('t', '')
    if not token:
        return "Invalid request", 400
    token_data = checkpoint_tokens.get(token)
    if not token_data:
        return "Invalid or expired checkpoint link", 403
    if token_data['used']:
        return "This checkpoint link has already been used", 403
    if time.time() > token_data['expires']:
        del checkpoint_tokens[token]
        return "Checkpoint link expired", 403

    client_ip = get_client_ip()
    if token_data['ip'] != client_ip:
        logger.warning(f"Checkpoint IP mismatch. Expected {token_data['ip']}, got {client_ip}")
        return "Session mismatch", 403

    checkpoint_tokens[token]['used'] = True
    script = next((s for s in scripts_data if s['id'] == token_data['script_id']), None)
    if not script:
        return "Script not found", 404

    if token_data['provider'] == 'lootlabs':
        link = script.get('lootlabs_link')
    elif token_data['provider'] == 'linkvertise':
        link = script.get('linkvertise_link')
    else:
        link = script.get('key_link')

    if not link:
        return "No verification link available", 404

    active_checkpoints[client_ip] = {
        'started': time.time(),
        'script_id': token_data['script_id'],
        'provider': token_data['provider'],
        'loaded': True
    }
    logger.info(f"Checkpoint loaded for IP: {client_ip}, redirecting to provider")
    return redirect(link)


@app.route('/checkpoint/done')
def checkpoint_done():
    client_ip = get_client_ip()
    referer = request.headers.get('Referer', '')
    timer_check = verification_timer.check_timer(client_ip)

    if not timer_check['valid']:
        logger.warning(f"Checkpoint done - invalid timer for IP: {client_ip}")
        return "Access denied - complete the key system first", 403

    cp_data = active_checkpoints.get(client_ip)
    if not cp_data or not cp_data.get('loaded'):
        if not is_valid_referrer(referer):
            logger.warning(f"Checkpoint done - no active checkpoint and invalid referer for IP: {client_ip}")
            return "No active checkpoint found - you must start from the key system page", 403
        logger.info(f"Checkpoint done - no active checkpoint but valid referer for IP: {client_ip}")
    else:
        time_in_checkpoint = time.time() - cp_data['started']
        if time_in_checkpoint < 10:
            logger.warning(f"Checkpoint done too fast for IP: {client_ip}, took {time_in_checkpoint:.1f}s")
            return "Verification completed too quickly - please try again properly", 403
        if not is_valid_referrer(referer):
            logger.info(f"Checkpoint done - valid checkpoint but missing referer for IP: {client_ip} (likely mobile)")
        del active_checkpoints[client_ip]

    now = time.time()
    expired = [k for k, v in active_checkpoints.items() if now - v['started'] > 600]
    for k in expired:
        del active_checkpoints[k]

    verification_timer.mark_verified(client_ip)
    token = secrets.token_urlsafe(32)
    verification_tokens[token] = {
        'ip': client_ip,
        'expires': time.time() + 300,
        'used': False
    }
    logger.info(f"Checkpoint completed for IP: {client_ip}, redirecting to verify")
    return redirect(f'/verify?token={token}')


@app.route('/verify')
def verify_page():
    client_ip = get_client_ip()
    try:
        with open('templates/verify.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
    except FileNotFoundError:
        logger.error("verify.html template not found")
        return jsonify({"error": "Verify page not found"}), 404

    html_content = html_content.replace('YOUR_SITE_KEY_HERE', TURNSTILE_SITE_KEY or '')
    token_param = request.args.get('token')

    if token_param:
        token_data = verification_tokens.get(token_param)
        if (token_data
                and not token_data['used']
                and time.time() <= token_data['expires']
                and token_data['ip'] == client_ip):
            html_content = html_content.replace(
                'let verificationToken = null;',
                f'let verificationToken = "{token_param}";'
            )
            logger.info(f"Verify page loaded with valid token for IP: {client_ip}")
            return html_content

    referer = request.headers.get('Referer', '')
    timer_check = verification_timer.check_timer(client_ip)

    if not timer_check['valid']:
        reason = timer_check.get('reason')
        if reason == 'time_not_elapsed':
            elapsed = timer_check.get('elapsed', 0)
            required = timer_check.get('required', 25)
            logger.warning(f"Timer bypass attempt from IP: {client_ip}. Only {elapsed:.1f}s elapsed (need {required}s)")
        elif reason == 'already_verified':
            logger.warning(f"Already verified IP trying again: {client_ip}")
        else:
            logger.warning(f"No timer found for IP: {client_ip}")
        return html_content

    if not is_valid_referrer(referer):
        logger.warning(f"Invalid referer from IP: {client_ip}. Referer: {referer}")
        return html_content

    verification_timer.mark_verified(client_ip)
    token = secrets.token_urlsafe(32)
    verification_tokens[token] = {
        'ip': client_ip,
        'expires': time.time() + 300,
        'used': False
    }
    html_content = html_content.replace(
        'let verificationToken = null;',
        f'let verificationToken = "{token}";'
    )
    logger.info(f"Verification token created for IP: {client_ip} after {timer_check['elapsed']:.1f}s")
    return html_content


@app.route('/create')
def create_key():
    token = request.args.get('token')
    captcha = request.args.get('captcha')
    if not token:
        logger.warning(f"Create attempt without token")
        return "Missing verification token", 403
    if not captcha:
        logger.warning(f"Create attempt without captcha")
        return "Missing captcha token", 403

    client_ip = get_client_ip()
    if not verify_turnstile(captcha, client_ip):
        logger.warning(f"Invalid captcha from IP: {client_ip}")
        return "Captcha verification failed", 403

    token_data = verification_tokens.get(token)
    if not token_data:
        logger.warning(f"Invalid token attempt from IP: {client_ip}")
        return "Invalid or expired token", 403
    if token_data['used']:
        logger.warning(f"Reused token attempt from IP: {client_ip}")
        return "Token already used", 403
    if time.time() > token_data['expires']:
        del verification_tokens[token]
        return "Token expired", 403
    if token_data['ip'] != client_ip:
        logger.warning(f"Token IP mismatch. Expected {token_data['ip']}, got {client_ip}")
        return "Session mismatch", 403

    verification_tokens[token]['used'] = True
    slug = key_system.create_slug(client_ip)
    host = request.headers.get('host', 'vadrifts.onrender.com')
    logger.info(f"Created key slug for IP: {client_ip}")
    return f"https://{host}/getkey/{slug}"


@app.route('/getkey/<slug>')
def get_key(slug):
    ip = key_system.get_ip_from_slug(slug)
    if not ip:
        logger.warning(f"Invalid or expired slug attempted: {slug}")
        return "Invalid or expired key link", 404
    key_system.consume_slug(slug)
    key = key_system.generate_key(ip)
    logger.info(f"Key generated for IP: {ip} - Key: {key}")
    return key


@app.route('/validate')
def validate_key_route():
    key = request.args.get('key')
    if not key:
        return "false"
    client_ip = get_client_ip()
    is_valid = key_system.validate_key(client_ip, key)
    return "true" if is_valid else "false"


@app.route('/api/validate-discord-key', methods=['POST'])
def validate_discord_key():
    data = request.get_json()

    if not data:
        return jsonify({"valid": False, "message": "No data provided"})

    secret = data.get("secret", "")
    key = data.get("key", "")
    hwid = data.get("hwid", "")

    if secret != DISCORD_KEY_API_SECRET:
        logger.warning(f"Discord key validation: wrong secret")
        return jsonify({"valid": False, "message": "Unauthorized"})

    if not key or not hwid:
        return jsonify({"valid": False, "message": "Missing key or HWID"})

    keys = load_discord_keys()
    logger.info(f"Discord key validation: loaded {len(keys)} keys from MongoDB")
    logger.info(f"Discord key validation: looking for key '{key[:8]}...'")
    logger.info(f"Discord key validation: available keys = {[k[:8]+'...' for k in keys.keys()]}")

    key_data = keys.get(key)

    if not key_data:
        logger.warning(f"Discord key validation: key not found")
        return jsonify({"valid": False, "message": "Invalid key"})

    logger.info(f"Discord key validation: key found, checking expiry")

    if time.time() > key_data.get("expires_at", 0):
        logger.warning(f"Discord key validation: key expired")
        del keys[key]
        save_discord_keys(keys)
        return jsonify({"valid": False, "message": "Key expired. Run /getkey in Discord."})

    discord_id = key_data.get("discord_id")
    logger.info(f"Discord key validation: checking membership for discord_id={discord_id}, guild={GUILD_ID}")
    logger.info(f"Discord key validation: using token '{DISCORD_TOKEN[:10]}...' (truncated)")

    is_member = check_discord_membership(discord_id)
    logger.info(f"Discord key validation: membership check = {is_member}")

    if not is_member:
        del keys[key]
        save_discord_keys(keys)
        return jsonify({"valid": False, "message": "You must be in the Discord server."})

    if key_data.get("hwid") and key_data["hwid"] != hwid:
        return jsonify({"valid": False, "message": "Key is locked to a different device. Use /resetkey in Discord."})

    if not key_data.get("hwid"):
        key_data["hwid"] = hwid
        keys[key] = key_data
        save_discord_keys(keys)

    logger.info(f"Discord key validation: SUCCESS")
    return jsonify({"valid": True, "message": "Authenticated"})


@app.route('/templates/<path:filename>')
def serve_templates(filename):
    return send_from_directory('templates', filename)


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory('static', filename)


@app.route('/discord')
def discord_invite():
    return redirect("https://discord.com/invite/WDbJ5wE2cR")


@app.route('/.well-known/discord')
def discord_verification():
    response = make_response('dh=6a7d0bee33f82bdb67f20d7ac5d8254e1a36cb64')
    response.headers['Content-Type'] = 'text/plain'
    return response


@app.route('/status-check')
def status_check():
    response = jsonify({
        "status": "operational",
        "timestamp": datetime.now().isoformat(),
        "code": "VADRIFTS_ONLINE_2025"
    })
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response, 200


@app.route('/health')
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()}), 200


@app.route('/api/scripts')
def get_scripts():
    search = request.args.get('search', '').lower()
    processed_scripts = process_script_data(scripts_data.copy())
    safe_scripts = [sanitize_script(s) for s in processed_scripts]
    if search:
        filtered_scripts = [
            script for script in safe_scripts
            if search in script.get('title', '').lower() or search in script.get('game', '').lower()
        ]
        return jsonify(filtered_scripts)
    return jsonify(safe_scripts)


@app.route('/api/scripts/<int:script_id>')
def get_script_detail(script_id):
    processed_scripts = process_script_data(scripts_data.copy())
    script = next((s for s in processed_scripts if s['id'] == script_id), None)
    if script:
        return jsonify(sanitize_script(script))
    return jsonify({"error": "Script not found"}), 404


@app.route('/convert-image', methods=['GET', 'POST'])
def convert_image():
    return convert_image_endpoint(request)


@app.route('/api/image/pixelate', methods=['GET', 'POST'])
def api_image_pixelate():
    return pixelate_endpoint(request)


@app.route('/api/image/invert', methods=['GET', 'POST'])
def api_image_invert():
    return invert_endpoint(request)


@app.route('/api/image/mirror', methods=['GET', 'POST'])
def api_image_mirror():
    return mirror_endpoint(request)


@app.route('/api/image/rotate', methods=['GET', 'POST'])
def api_image_rotate():
    return rotate_endpoint(request)


@app.route('/api/image/format', methods=['GET', 'POST'])
def api_image_format():
    return format_endpoint(request)


@app.route('/api/image/remove-bg', methods=['GET', 'POST'])
def api_image_remove_bg():
    return remove_bg_endpoint(request)


@app.route('/api/find-channels', methods=['POST'])
def find_channels():
    data = request.get_json()
    usernames = data.get('usernames', [])
    return youtube_finder.find_multiple_channels(usernames)


@app.route('/api/showcasers')
def showcasers():
    # Static, cacheable, and instant. The homepage uses this instead of the
    # live YouTube scraper so a throttled/slow YouTube can never hang the site.
    from showcasers_data import SHOWCASERS
    resp = jsonify({'channels': SHOWCASERS})
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


def _valid_lootlabs_referrer(referer):
    if not referer:
        return True  # Some mobile/privacy browsers intentionally omit Referer.
    try:
        host = (urlparse(referer).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    allowed = ("lootlabs.gg", "lootdest.org", "loot-link.com")
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def _render_renewal_page(session_token, error=None, status_code=200):
    renewal_session = get_renewal_session(session_token)
    if not renewal_session:
        response = make_response(
            render_template(
                "guild-renewal.html",
                expired=True,
                error=error,
                checkpoint_count=CHECKPOINT_COUNT,
            ),
            403,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    entitlement = get_renewal_entitlement(renewal_session["guild_id"]) or {}
    access = get_renewal_status(renewal_session["guild_id"])
    timezone_name = entitlement.get("timezone", "UTC")
    due_text = format_renewal_timestamp(entitlement.get("due_at"), timezone_name)
    grace_text = format_renewal_timestamp(
        entitlement.get("grace_ends_at"), timezone_name
    )
    response = make_response(
        render_template(
            "guild-renewal.html",
            expired=False,
            error=error,
            notice=(
                "Checkpoint recorded. Continue with the next checkpoint."
                if request.args.get("step") == "complete" and not renewal_session.get("completed")
                else None
            ),
            guild_name=entitlement.get("guild_name", "Discord server"),
            session_token=session_token,
            completed_steps=renewal_session.get("completed_steps", []),
            current_step=int(renewal_session.get("current_step", 1)),
            checkpoint_count=CHECKPOINT_COUNT,
            session_completed=bool(renewal_session.get("completed")),
            access_state=access.get("state", "active"),
            due_text=due_text,
            grace_text=grace_text,
        ),
        status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.route('/ks/renew/<session_token>')
def ks_renewal_page(session_token):
    return _render_renewal_page(session_token)


@app.route('/ks/renew/<session_token>/checkpoint')
def ks_renewal_checkpoint(session_token):
    try:
        loot_url, step = start_renewal_checkpoint(
            session_token,
            get_client_ip(),
            base_url=SERVER_BASE_URL,
        )
        logger.info("Guild renewal checkpoint started: step=%s", step)
        return redirect(loot_url)
    except ValueError as exc:
        return _render_renewal_page(session_token, str(exc), 403)
    except Exception as exc:
        logger.exception("Could not start guild renewal checkpoint")
        return _render_renewal_page(
            session_token,
            str(exc) if str(exc) else "Could not start this checkpoint. Try again shortly.",
            503,
        )


@app.route('/ks/renew/complete/<session_token>/<int:step>/<completion_token>')
def ks_renewal_checkpoint_complete(session_token, step, completion_token):
    referer = request.headers.get("Referer", "")
    if not _valid_lootlabs_referrer(referer):
        logger.warning("Rejected renewal completion with referrer %r", referer)
        return _render_renewal_page(
            session_token,
            "Return through the LootLabs checkpoint instead of opening the completion URL directly.",
            403,
        )
    try:
        result = complete_renewal_checkpoint(
            session_token,
            step,
            completion_token,
            get_client_ip(),
        )
    except ValueError as exc:
        return _render_renewal_page(session_token, str(exc), 403)
    except Exception:
        logger.exception("Could not complete guild renewal checkpoint")
        return _render_renewal_page(
            session_token,
            "The checkpoint could not be saved. Reload and try again.",
            503,
        )

    if result.get("completed"):
        return redirect(f"/ks/renew/{session_token}?renewed=1")
    return redirect(f"/ks/renew/{session_token}?step=complete")


def _render_result(icon, title, message, page_class="success", page_title="Key System"):
    try:
        with open('templates/keysystem_result.html', 'r', encoding='utf-8') as f:
            html = f.read()
        html = html.replace('{{ICON}}', icon)
        html = html.replace('{{TITLE}}', title)
        html = html.replace('{{MESSAGE}}', message)
        html = html.replace('{{PAGE_CLASS}}', page_class)
        html = html.replace('{{PAGE_TITLE}}', page_title)
        return html
    except FileNotFoundError:
        return f"<h1>{title}</h1><p>{message}</p>"


def _renewal_denied_response(guild_id):
    status = get_renewal_status(guild_id)
    if status.get("allows_access", False):
        return None
    if status.get("state") == "unavailable":
        message = "Service access could not be checked. Please try again shortly."
    else:
        message = (
            "This server's service access expired. Existing keys remain stored, "
            "but a server admin must complete all four renewal checkpoints from /ks setup."
        )
    return _render_result(
        '⛔', 'Server Renewal Required', message, 'error', 'Renewal Required'
    ), 403


@app.route('/ks/gateway/<session_token>')
def ks_gateway(session_token):
    session = get_session(session_token)
    if not session:
        return _render_result(
            '❌', 'Invalid Session',
            'This session has expired or does not exist. Run the key command again in Discord.',
            'error', 'Session Expired'
        ), 403

    renewal_denied = _renewal_denied_response(session['guild_id'])
    if renewal_denied:
        return renewal_denied

    profile = get_script_profile(session['profile_id'])
    if not profile or not profile.get('enabled'):
        return _render_result(
            '❌', 'Key System Disabled',
            'This script profile is not active.',
            'error', 'Disabled'
        ), 403

    guild_config = get_guild_config(session['guild_id'])
    if not guild_config or not guild_config.get('enabled'):
        return _render_result(
            '❌', 'Key System Disabled',
            'The key system is not active for this server.',
            'error', 'Disabled'
        ), 403

    client_ip = get_client_ip()
    update_session(session_token, {"ip": client_ip})

    try:
        with open('templates/keysystem_gateaway.html', 'r', encoding='utf-8') as f:
            html = f.read()
    except FileNotFoundError:
        return "Gateway template not found", 500

    html = html.replace('{{SESSION_TOKEN}}', session_token)
    html = html.replace('{{GUILD_ID}}', session['guild_id'])
    html = html.replace('{{GUILD_NAME}}', guild_config.get('guild_name', 'Server'))
    html = html.replace('{{PROFILE_NAME}}', profile.get('name', 'Script'))

    html = html.replace('{{WORKINK_DISABLED}}',
        '' if profile.get('workink_url') else 'disabled')
    html = html.replace('{{LOOTLABS_DISABLED}}',
        '' if profile.get('lootlabs_url') else 'disabled')
    html = html.replace('{{LINKVERTISE_DISABLED}}',
        '' if profile.get('linkvertise_url') else 'disabled')

    return html


@app.route('/ks/timer/<session_token>')
def ks_timer(session_token):
    session = get_session(session_token)
    if not session:
        return jsonify({"success": False, "error": "Invalid session"})
    renewal = get_renewal_status(session['guild_id'])
    if not renewal.get("allows_access", False):
        return jsonify({
            "success": False,
            "error": "Server service access expired; ask an admin to renew it."
        }), 403

    client_ip = get_client_ip()

    if session.get('ip') and session['ip'] != client_ip:
        logger.warning(f"KS timer IP mismatch: session={session['ip']}, request={client_ip}")
        return jsonify({"success": False, "error": "Session mismatch"})

    update_session(session_token, {
        "timer_started": True,
        "timer_started_at": time.time(),
        "ip": client_ip
    })

    logger.info(f"KS timer started: guild={session['guild_id']}, user={session['discord_name']}, ip={client_ip}")
    return jsonify({"success": True})


@app.route('/ks/redirect/<session_token>/<provider>')
def ks_redirect(session_token, provider):
    session = get_session(session_token)
    if not session:
        return _render_result(
            '❌', 'Invalid Session',
            'Session expired. Run the key command again in Discord.',
            'error'
        ), 403

    renewal_denied = _renewal_denied_response(session['guild_id'])
    if renewal_denied:
        return renewal_denied

    if not session.get('timer_started'):
        return _render_result(
            '❌', 'Timer Not Started',
            'Please load the gateway page first.',
            'error'
        ), 403

    profile = get_script_profile(session['profile_id'])
    if not profile:
        return _render_result('❌', 'Error', 'Script profile not found.', 'error'), 404

    provider_map = {
        'workink': profile.get('workink_url'),
        'lootlabs': profile.get('lootlabs_url'),
        'linkvertise': profile.get('linkvertise_url'),
    }

    url = provider_map.get(provider)
    if not url:
        return _render_result(
            '❌', 'Provider Unavailable',
            'This verification provider is not configured.',
            'error'
        ), 404

    update_session(session_token, {"provider_used": provider})
    logger.info(f"KS redirect: user={session['discord_name']}, provider={provider}")
    return redirect(url)


@app.route('/ks/done/<guild_id>/<profile_id>')
def ks_done(guild_id, profile_id):
    renewal_denied = _renewal_denied_response(guild_id)
    if renewal_denied:
        return renewal_denied

    guild_config = get_guild_config(guild_id)
    if not guild_config or not guild_config.get('enabled'):
        return _render_result(
            '❌', 'Invalid Server',
            'Key system not found or disabled.',
            'error', 'Error'
        ), 404

    profile = get_script_profile(profile_id)
    if not profile or not profile.get('enabled'):
        return _render_result(
            '❌', 'Invalid Profile',
            'Script profile not found or disabled.',
            'error', 'Error'
        ), 404

    if profile.get('guild_id') != str(guild_id):
        return _render_result(
            '❌', 'Mismatch',
            'Profile does not belong to this server.',
            'error', 'Error'
        ), 403

    client_ip = get_client_ip()
    referer = request.headers.get('Referer', '')

    session = find_session_by_ip_and_profile(client_ip, guild_id, profile_id)
    if not session:
        logger.warning(f"KS done: no matching session for IP={client_ip}, guild={guild_id}, profile={profile_id}")
        return _render_result(
            '❌', 'No Active Session',
            'No verification session found. Please start from Discord.',
            'error', 'Access Denied'
        ), 403

    timer_started_at = session.get('timer_started_at')

    if not timer_started_at:
        return _render_result(
            '❌', 'Timer Error',
            'Verification timer was not started. Please try again.',
            'error', 'Error'
        ), 403

    elapsed = time.time() - timer_started_at
    if elapsed < MIN_COMPLETION_SECONDS:
        logger.warning(f"KS done: too fast. {elapsed:.1f}s < {MIN_COMPLETION_SECONDS}s, IP={client_ip}")
        return _render_result(
            '⚠️', 'Too Fast',
            f'Verification completed too quickly ({elapsed:.0f}s). Please try again properly.',
            'error', 'Verification Failed'
        ), 403

    if referer and not is_valid_referrer(referer):
        logger.warning(f"KS done: suspicious referer from IP={client_ip}: {referer}")
        return _render_result(
            '❌', 'Invalid Access',
            'You must complete the verification task through the provided link.',
            'error', 'Access Denied'
        ), 403

    if not referer:
        logger.info(f"KS done: no referer (likely mobile), allowing. IP={client_ip}")

    update_session(session['token'], {
        "completed": True,
        "completed_at": time.time()
    })

    logger.info(f"KS done: session completed. user={session['discord_name']}, guild={guild_id}, profile={profile_id}, elapsed={elapsed:.1f}s")

    return _render_result(
        '✅', 'Verification Complete!',
        'Return to Discord and click <span class="highlight">Claim Key</span> to get your key.',
        'success', 'Verified!'
    )


@app.route('/ks/status/<session_token>')
def ks_status(session_token):
    session = get_session(session_token)
    if not session:
        return jsonify({"exists": False, "completed": False})
    renewal = get_renewal_status(session['guild_id'])
    return jsonify({
        "exists": True,
        "completed": session.get("completed", False),
        "key_claimed": session.get("key_claimed", False),
        "timer_started": session.get("timer_started", False),
        "sponsored_access": renewal.get("state"),
        "access_allowed": renewal.get("allows_access", False),
    })

def _guild_validation_code(valid, message):
    """Stable client-facing code; human-readable messages may evolve freely."""
    if valid:
        return "authenticated"
    return {
        "Key system unavailable": "service_unavailable",
        "Invalid API secret": "invalid_configuration",
        "This server's service access expired. Ask an admin to renew it in Discord.": "sponsored_renewal_required",
        "Server sponsored access expired. Ask an admin to renew it in Discord.": "sponsored_renewal_required",
        "Invalid key": "invalid_key",
        "Key expired. Get a new one from Discord.": "key_expired",
        "Key locked to another device.": "hwid_mismatch",
        "You must be in the Discord server.": "membership_required",
        "Validation error": "service_unavailable",
    }.get(message, "validation_failed")


@app.route('/api/validate-guild-key', methods=['POST', 'GET'])
def validate_guild_key_route():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        if not data:
            return jsonify({
                "valid": False,
                "code": "invalid_request",
                "message": "No data provided",
            })
        key = data.get("key", "")
        hwid = data.get("hwid", "")
        secret = data.get("secret", "")
    else:
        key = request.args.get("key", "")
        hwid = request.args.get("hwid", "")
        secret = request.args.get("secret", "")

    if not key or not hwid or not secret:
        return jsonify({
            "valid": False,
            "code": "invalid_request",
            "message": "Missing key, HWID, or secret",
        })

    valid, message = validate_guild_key(key, hwid, secret)
    code = _guild_validation_code(valid, message)
    logger.info(
        "Guild key validation (%s): key='%s...' valid=%s code='%s' message='%s'",
        request.method,
        key[:8],
        valid,
        code,
        message,
    )
    return jsonify({"valid": valid, "code": code, "message": message})

SUGGESTION_WEBHOOK_URL = os.environ.get("SUGGESTION_WEBHOOK_URL")
SUGGESTION_MIN_LENGTH = 8
SUGGESTION_MAX_LENGTH = 800
SUGGESTION_IP_COOLDOWN_SECONDS = 25
SUGGESTION_IP_HOURLY_CAP = 6
SUGGESTION_TYPE_META = {
    "idea":     {"label": "IDEA",     "color": 0xC77DFF},
    "bug":      {"label": "BUG",      "color": 0xEF476F},
    "feedback": {"label": "FEEDBACK", "color": 0x4CC9F0},
}
_suggestion_history = defaultdict(list)


def _suggestion_client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


@app.route('/api/suggestion', methods=['POST'])
def submit_suggestion_route():
    if not SUGGESTION_WEBHOOK_URL:
        return jsonify({"ok": False, "error": "Suggestions are not configured on the server."}), 503

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    stype = (data.get("type") or "idea").lower()

    if stype not in SUGGESTION_TYPE_META:
        stype = "idea"
    if len(text) < SUGGESTION_MIN_LENGTH:
        return jsonify({"ok": False, "error": "Add a few more words \u2014 keep it meaningful."}), 400
    if len(text) > SUGGESTION_MAX_LENGTH:
        return jsonify({"ok": False, "error": "Too long. Keep it under 800 characters."}), 400

    ip = _suggestion_client_ip()
    now = time.time()
    history = [t for t in _suggestion_history[ip] if now - t < 3600]
    _suggestion_history[ip] = history

    if history and now - history[-1] < SUGGESTION_IP_COOLDOWN_SECONDS:
        wait = int(SUGGESTION_IP_COOLDOWN_SECONDS - (now - history[-1]))
        return jsonify({"ok": False, "error": f"Slow down \u2014 try again in {wait}s."}), 429
    if len(history) >= SUGGESTION_IP_HOURLY_CAP:
        return jsonify({"ok": False, "error": "Hourly limit hit. Try again later."}), 429

    meta = SUGGESTION_TYPE_META[stype]
    embed = {
        "title": f"New {meta['label']} Suggestion",
        "description": text,
        "color": meta["color"],
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        r = requests.post(
            SUGGESTION_WEBHOOK_URL,
            json={"username": "vadrifts", "embeds": [embed]},
            timeout=8,
        )
        if r.status_code >= 300:
            logger.warning(f"Suggestion webhook returned {r.status_code}: {r.text[:200]}")
            return jsonify({"ok": False, "error": "Discord didn't accept that. Try again."}), 502
    except requests.RequestException as exc:
        logger.exception(f"Suggestion webhook failed: {exc}")
        return jsonify({"ok": False, "error": "Could not reach Discord. Try again."}), 502

    _suggestion_history[ip].append(now)
    return jsonify({"ok": True})

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    # threaded=True is critical: the Flask dev server is single-threaded by
    # default, so one slow request (YouTube scrape, image processing, Discord
    # API, DB connect) would block every other page load and cause timeouts/503s.
    threading.Thread(target=server_pinger, daemon=True).start()
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
