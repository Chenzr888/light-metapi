"""Domain implementation loaded into the shared Flask application namespace."""

def user_count():
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user_by_username(username):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def setup_login(user):
    session.permanent = True
    session["id"] = user["id"]
    session["username"] = user["username"]
    session["totp_enabled"] = bool(user["totp_enabled"])


def clear_login():
    session.clear()


def current_user():
    user_id = session.get("id")
    if not user_id:
        return None
    user = get_user(user_id)
    if not user:
        clear_login()
        return None
    return user


def auth_state(user=None):
    active_user = user if user is not None else current_user()
    state = {
        "needs_setup": user_count() == 0,
        "authenticated": bool(active_user),
        "username": active_user["username"] if active_user else "",
        "totp_enabled": bool(active_user["totp_enabled"]) if active_user else False,
    }
    return state


def login_attempt_key(username):
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
    client_address = forwarded or request.remote_addr or "unknown"
    return auth_security.attempt_key(username, client_address)


def login_rate_limit_response(retry_after):
    response = jsonify({"ok": False, "message": "登录尝试过多，请稍后再试"})
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if user_count() == 0:
            return response_error("管理员账号尚未预设", 401)
        if not current_user():
            return response_error("请先登录", 401)
        return fn(*args, **kwargs)
    return wrapper


def random_totp_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def normalize_totp_secret(secret):
    return (secret or "").strip().replace(" ", "").upper()


def hotp(secret, counter, digits=6):
    normalized = normalize_totp_secret(secret)
    padded = normalized + "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(padded, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret, code, window=1):
    value = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(value) != 6:
        return False
    counter = int(time.time() // 30)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(hotp(secret, counter + drift), value):
            return True
    return False


def totp_uri(username, secret):
    label = quote(f"light-metapi:{username}")
    issuer = quote("light-metapi")
    return f"otpauth://totp/{label}?secret={normalize_totp_secret(secret)}&issuer={issuer}&digits=6&period=30"
