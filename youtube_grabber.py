import requests
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from flask import jsonify
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

PER_USER_TIMEOUT = 8

BATCH_TIMEOUT = 12

MAX_WORKERS = 8


class YouTubeChannelFinder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        self.cache = {}
        self.cache_duration = 7200

    def _fallback(self, username):
        return {
            'name': username,
            'handle': f'@{username}',
            'url': f'https://www.youtube.com/@{username}',
            'pfp_url': None,
            'found': False,
        }

    def find_channel_by_username(self, username):
        username = (username or '').strip()
        if not username:
            return self._fallback('')

        cache_key = username.lower()
        cached = self.cache.get(cache_key)
        if cached and time.time() - cached['timestamp'] < self.cache_duration:
            return cached['data']

        data = self._find_channel_blocking(username)
        self.cache[cache_key] = {
            'data': data,
            'timestamp': time.time(),
        }
        return data

    def _find_channel_blocking(self, username):
        possible_urls = [
            f"https://www.youtube.com/@{username}",
            f"https://www.youtube.com/c/{username}",
            f"https://www.youtube.com/user/{username}",
            f"https://www.youtube.com/{username}",
        ]

        for url in possible_urls:
            try:
                response = self.session.get(
                    url, timeout=(3, 5), allow_redirects=True
                )
                if response.status_code == 200 and 'youtube.com' in response.url:
                    channel_data = self.extract_channel_data(
                        response.text, response.url, username
                    )
                    if channel_data:
                        return channel_data
            except Exception as e:
                logger.debug(f"Failed to fetch {url}: {e}")
                continue

        search_result = self.search_for_channel(username)
        if search_result:
            return search_result

        logger.warning(f"Could not find channel for username: {username}")
        return self._fallback(username)

    def extract_channel_data(self, html_content, url, username):
        try:
            channel_name = self.extract_channel_name(html_content)
            pfp_url = self.extract_profile_picture(html_content)

            handle = f'@{username}'
            if '"canonicalChannelUrl":"https://www.youtube.com/@' in html_content:
                handle_match = re.search(
                    r'"canonicalChannelUrl":"https://www\\.youtube\\.com/@([^"]+)"',
                    html_content,
                )
                if handle_match:
                    handle = f'@{handle_match.group(1)}'

            return {
                'name': channel_name or username,
                'handle': handle,
                'url': url,
                'pfp_url': pfp_url,
                'found': True,
            }
        except Exception as e:
            logger.error(f"Error extracting channel data: {e}")
            return None

    def extract_channel_name(self, html_content):
        patterns = [
            r'"channelMetadataRenderer":{"title":"([^"]+)"',
            r'<meta property="og:title" content="([^"]+)"',
            r'"title":"([^"]+)","navigationEndpoint"',
            r'<title>([^<]+)</title>',
        ]

        for pattern in patterns:
            match = re.search(pattern, html_content)
            if match:
                name = match.group(1).strip()
                if name and name != 'YouTube':
                    return name
        return None

    def extract_profile_picture(self, html_content):
        patterns = [
            r'"avatar".*?"thumbnails".*?"url":"([^"]+)".*?"width":176',
            r'"avatar".*?"thumbnails".*?"url":"([^"]+)"',
            r'<link itemprop="thumbnailUrl" href="([^"]+)"',
            r'<meta property="og:image" content="([^"]+)"',
            r'"thumbnailUrl":\s*"([^"]+)"',
        ]

        for pattern in patterns:
            matches = re.finditer(pattern, html_content)
            for match in matches:
                img_url = match.group(1)
                img_url = img_url.replace('\\u003d', '=').replace('\\', '')

                if self._looks_like_image_url(img_url):
                    return img_url

        yt3_pattern = r'(https?://yt3\.ggpht\.com[^"]*)'
        for url in re.findall(yt3_pattern, html_content):
            if self._looks_like_image_url(url):
                return url

        return None

    @staticmethod
    def _looks_like_image_url(url):
        if not url or not url.startswith('http'):
            return False
        host_ok = (
            'yt3.ggpht.com' in url
            or 'yt.googleusercontent.com' in url
            or 'googleusercontent.com' in url
            or url.endswith(('.jpg', '.jpeg', '.png', '.webp'))
        )
        return host_ok

    def search_for_channel(self, username):
        try:
            search_url = (
                f"https://www.youtube.com/results?search_query="
                f"{quote_plus(username)}&sp=EgIQAg%253D%253D"
            )
            response = self.session.get(search_url, timeout=(3, 5))

            if response.status_code != 200:
                return None

            channel_links = re.findall(r'"url":"(/channel/[^"]+)"', response.text)
            handle_links = re.findall(r'"url":"(/@[^"]+)"', response.text)

            all_links = []
            for link in channel_links + handle_links:
                if link.startswith('/'):
                    all_links.append(f"https://www.youtube.com{link}")

            for link in all_links[:2]:
                try:
                    channel_response = self.session.get(
                        link, timeout=(3, 5)
                    )
                    if channel_response.status_code == 200:
                        channel_data = self.extract_channel_data(
                            channel_response.text, link, username
                        )
                        if channel_data and channel_data.get('pfp_url'):
                            return channel_data
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"Search failed for {username}: {e}")

        return None

    def find_multiple_channels(self, usernames):
        if not usernames:
            return jsonify({'error': 'No usernames provided'}), 400

        cleaned = []
        seen = set()
        for u in usernames:
            u = (u or '').strip()
            if u and u.lower() not in seen:
                seen.add(u.lower())
                cleaned.append(u)

        results = {u: self._fallback(u) for u in cleaned}

        live = []
        for u in cleaned:
            cached = self.cache.get(u.lower())
            if cached and time.time() - cached['timestamp'] < self.cache_duration:
                results[u] = cached['data']
            else:
                live.append(u)

        if live:
            pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(live)))
            try:
                future_to_user = {
                    pool.submit(self._find_channel_blocking, u): u for u in live
                }
                deadline = time.time() + BATCH_TIMEOUT
                for future, user in future_to_user.items():
                    remaining = max(0.1, deadline - time.time())
                    try:
                        results[user] = future.result(timeout=remaining)
                    except FuturesTimeout:
                        logger.warning(
                            f"YouTube lookup timed out for '{user}'"
                        )
                        future.cancel()
                        results[user] = self._fallback(user)
                    except Exception as e:
                        logger.warning(
                            f"YouTube lookup failed for '{user}': {e}"
                        )
                        results[user] = self._fallback(user)
            finally:

                pool.shutdown(wait=False)

        channels = [results[u] for u in cleaned]
        return jsonify({'channels': channels})
