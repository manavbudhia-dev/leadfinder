"""Per-provider rate limiting: lp.PROVIDERS functions ko throttle wrapper se
wrap karta hai (lead_pipeline.py file unchanged rehti hai). Parallel batch
mein bhi har provider ki apni minimum request interval maintain hoti hai.
"""
import os
import threading
import time

import lead_pipeline as lp

_last_call = {}


def min_interval():
    return float(os.getenv("PROVIDER_MIN_INTERVAL", "0.5"))


def _throttled(name, func):
    lock = threading.Lock()

    def call(*args, **kwargs):
        with lock:
            wait = min_interval() - (time.monotonic() - _last_call.get(name, 0.0))
            if wait > 0:
                time.sleep(wait)
            _last_call[name] = time.monotonic()
        return func(*args, **kwargs)

    return call


def wrap_providers():
    lp.PROVIDERS = [
        (name, _throttled(name, func), env, default)
        for name, func, env, default in lp.PROVIDERS
    ]