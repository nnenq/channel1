import hashlib
import hmac
import json
import time
import urllib.parse

TOKEN = "123:ABC"


def init_data(uid):
    """Подписанные initData Telegram WebApp для тестов API."""
    d = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": "T"})}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    d["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(d)
