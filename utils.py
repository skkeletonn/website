import os
import time
import requests
import logging

logger = logging.getLogger(__name__)

SITE_URLS = [
    os.environ.get("SITE_URL", "https://valorium.onrender.com").rstrip("/"),
    os.environ.get("SITE_URL_2", "https://vadrifts.onrender.com").rstrip("/"),
]


def inject_meta_tags(html_content, meta_tags):
    if '<head>' in html_content:
        return html_content.replace('<head>', f'<head>\n{meta_tags}')
    return html_content


def server_pinger():
    """Ping both servers' /health endpoints every 5 minutes."""
    while True:
        for site_url in SITE_URLS:
            health_url = f"{site_url}/health"

            try:
                response = requests.get(health_url, timeout=10)
                logger.debug(
                    f"Pinged {health_url} — status {response.status_code}"
                )
            except Exception as e:
                logger.debug(f"Ping failed for {health_url}: {e}")

        time.sleep(300)
