import hashlib
import time
import threading
import random
import string
from datetime import datetime

class KeySystemManager:
    def __init__(self):
        self.active_slugs = {}
    
    def generate_key(self, ip):
        period = int(time.time() // (60 * 60 * 48))
        hash_input = f"{ip}{period}"
        return hashlib.sha256(hash_input.encode()).hexdigest()[:16]
    
    def create_slug(self, ip):
        slug = ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
        self.active_slugs[slug] = ip
        
        def remove_slug():
            if slug in self.active_slugs:
                del self.active_slugs[slug]
        
        timer = threading.Timer(300, remove_slug)
        timer.start()
        
        return slug
    
    def get_ip_from_slug(self, slug):
        return self.active_slugs.get(slug)
    
    def consume_slug(self, slug):
        if slug in self.active_slugs:
            del self.active_slugs[slug]

    def validate_key(self, ip, key):
        expected_key = self.generate_key(ip)
        return key == expected_key
