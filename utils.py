import time
import requests
import logging

logger = logging.getLogger(__name__)

def inject_meta_tags(html_content, meta_tags):
    if '<head>' in html_content:
        return html_content.replace('<head>', f'<head>\n{meta_tags}')
    return html_content

def server_pinger():
    while True:
        try:
            requests.get("https://valorium.onrender.com/health", timeout=10)
        except Exception as e:
            logger.error(f"Ping failed: {e}")
        time.sleep(300)
