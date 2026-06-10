from slowapi import Limiter
from slowapi.util import get_remote_address

# Single shared limiter instance imported by api.py and all route modules.
# key_func=get_remote_address buckets limits per client IP.
limiter = Limiter(key_func=get_remote_address)
