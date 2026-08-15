import os
import time
import requests
import logging

logger = logging.getLogger(__name__)

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")


def inject_meta_tags(html_content, meta_tags):
    if '<head>' in html_content:
        return html_content.replace('<head>', f'<head>\n{meta_tags}')
    return html_content


def server_pinger():
    """Keep the Render free instance awake by hitting its own /health endpoint.

    Note: Render free instances can still be forced to sleep regardless of
    pings; this just reduces cold starts. Previously this pinged the WRONG
    host (valorium.onrender.com), so it did nothing for this service.
    """
    health_url = f"{SITE_URL}/health"
    while True:
        try:
            requests.get(health_url, timeout=10)
        except Exception as e:
            logger.debug(f"Self-ping failed: {e}")
        time.sleep(300)
