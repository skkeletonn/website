import time
import threading
from typing import Dict, Any
from datetime import datetime

class VerificationTimer:
    def __init__(self, min_verification_time=25):
        self.ip_timers = {}
        self.min_verification_time = min_verification_time
        self.cleanup_interval = 3600
        self.start_cleanup_thread()
    
    def start_cleanup_thread(self):
        def cleanup_loop():
            while True:
                self.cleanup_expired_timers()
                time.sleep(self.cleanup_interval)
        
        thread = threading.Thread(target=cleanup_loop, daemon=True)
        thread.start()
    
    def start_timer(self, ip):
        self.ip_timers[ip] = {
            'started_at': time.time(),
            'verified': False
        }
    
    def check_timer(self, ip):
        if ip not in self.ip_timers:
            return {'valid': False, 'reason': 'no_timer'}
        
        timer_data = self.ip_timers[ip]
        
        if timer_data['verified']:
            return {'valid': False, 'reason': 'already_verified'}
        
        elapsed = time.time() - timer_data['started_at']
        
        if elapsed < self.min_verification_time:
            return {
                'valid': False, 
                'reason': 'time_not_elapsed',
                'elapsed': elapsed,
                'required': self.min_verification_time
            }
        
        return {'valid': True, 'elapsed': elapsed}
    
    def mark_verified(self, ip):
        if ip in self.ip_timers:
            self.ip_timers[ip]['verified'] = True
            return True
        return False
    
    def cleanup_expired_timers(self):
        current_time = time.time()
        expired_time = 24 * 3600
        
        ips_to_remove = [
            ip for ip, data in self.ip_timers.items()
            if current_time - data['started_at'] > expired_time
        ]
        
        for ip in ips_to_remove:
            if ip in self.ip_timers:
                del self.ip_timers[ip]
