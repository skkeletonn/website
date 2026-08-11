import requests
import re
import time
import logging
from flask import jsonify
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

class YouTubeChannelFinder:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        self.cache = {}
        self.cache_duration = 7200
    
    def find_channel_by_username(self, username):
        cache_key = username.lower().strip()
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached['timestamp'] < self.cache_duration:
                return cached['data']
        
        possible_urls = [
            f"https://www.youtube.com/@{username}",
            f"https://www.youtube.com/c/{username}",
            f"https://www.youtube.com/user/{username}",
            f"https://www.youtube.com/{username}"
        ]
        
        for url in possible_urls:
            try:
                response = self.session.get(url, timeout=10, allow_redirects=True)
                
                if response.status_code == 200 and 'youtube.com' in response.url:
                    channel_data = self.extract_channel_data(response.text, response.url, username)
                    if channel_data:
                        self.cache[cache_key] = {
                            'data': channel_data,
                            'timestamp': time.time()
                        }
                        return channel_data
                        
            except Exception as e:
                logger.debug(f"Failed to fetch {url}: {e}")
                continue
        
        search_result = self.search_for_channel(username)
        if search_result:
            self.cache[cache_key] = {
                'data': search_result,
                'timestamp': time.time()
            }
            return search_result
        
        logger.warning(f"Could not find channel for username: {username}")
        return {
            'name': username,
            'handle': f'@{username}',
            'url': f'https://www.youtube.com/@{username}',
            'pfp_url': None,
            'found': False
        }
    
    def extract_channel_data(self, html_content, url, username):
        try:
            channel_name = self.extract_channel_name(html_content)
            pfp_url = self.extract_profile_picture(html_content)
            
            handle = f'@{username}'
            if '"canonicalChannelUrl":"https://www.youtube.com/@' in html_content:
                handle_match = re.search(r'"canonicalChannelUrl":"https://www\.youtube\.com/@([^"]+)"', html_content)
                if handle_match:
                    handle = f'@{handle_match.group(1)}'
            
            return {
                'name': channel_name or username,
                'handle': handle,
                'url': url,
                'pfp_url': pfp_url,
                'found': True
            }
        except Exception as e:
            logger.error(f"Error extracting channel data: {e}")
            return None
    
    def extract_channel_name(self, html_content):
        patterns = [
            r'"channelMetadataRenderer":{"title":"([^"]+)"',
            r'<meta property="og:title" content="([^"]+)"',
            r'"title":"([^"]+)","navigationEndpoint"',
            r'<title>([^<]+)</title>'
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
            r'"thumbnailUrl":\s*"([^"]+)"'
        ]
        
        for pattern in patterns:
            matches = re.finditer(pattern, html_content)
            for match in matches:
                img_url = match.group(1)
                img_url = img_url.replace('\\u003d', '=').replace('\\', '')
                
                if self.validate_image_url(img_url):
                    return img_url
        
        yt3_pattern = r'(https?://yt3\.ggpht\.com[^"]*)'
        matches = re.findall(yt3_pattern, html_content)
        for url in matches:
            if self.validate_image_url(url):
                return url
        
        return None
    
    def search_for_channel(self, username):
        try:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(username)}&sp=EgIQAg%253D%253D"
            response = self.session.get(search_url, timeout=10)
            
            if response.status_code == 200:
                channel_links = re.findall(r'"url":"(/channel/[^"]+)"', response.text)
                handle_links = re.findall(r'"url":"(/@[^"]+)"', response.text)
                
                all_links = []
                for link in channel_links + handle_links:
                    if link.startswith('/'):
                        all_links.append(f"https://www.youtube.com{link}")
                
                for link in all_links[:3]:
                    try:
                        channel_response = self.session.get(link, timeout=10)
                        if channel_response.status_code == 200:
                            channel_data = self.extract_channel_data(channel_response.text, link, username)
                            if channel_data and channel_data.get('pfp_url'):
                                return channel_data
                    except:
                        continue
        except Exception as e:
            logger.error(f"Search failed for {username}: {e}")
        
        return None
    
    def validate_image_url(self, url):
        try:
            if not url or not url.startswith('http'):
                return False
            
            response = self.session.head(url, timeout=5)
            content_type = response.headers.get('content-type', '').lower()
            
            return (response.status_code == 200 and 
                    ('image' in content_type or url.endswith(('.jpg', '.jpeg', '.png', '.webp'))))
        except:
            return False
    
    def find_multiple_channels(self, usernames):
        if not usernames:
            return jsonify({'error': 'No usernames provided'}), 400
        
        channels = []
        for username in usernames:
            username = username.strip()
            if username:
                channel_data = self.find_channel_by_username(username)
                channels.append(channel_data)
        
        return jsonify({'channels': channels})
