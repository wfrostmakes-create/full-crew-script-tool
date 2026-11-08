import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("SCRIPT_TOOL_DATA_DIR", ROOT / "data"))
STATE_FILE = DATA_DIR / "state.json"
SESSION_COOKIE = "script_session"
SESSION_TTL = 60 * 60 * 24 * 7
PASSWORD_ITERATIONS = 310_000
DEFAULT_ROLES = ["Actor", "Director", "Stage Manager", "Crew"]
sessions = {}
state_lock = threading.RLock()


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return {
        "salt": salt.hex(),
        "hash": derived.hex(),
        "iterations": PASSWORD_ITERATIONS,
    }


def verify_password(password, password_data):
    try:
        salt = bytes.fromhex(password_data["salt"])
        expected = bytes.fromhex(password_data["hash"])
        iterations = int(password_data["iterations"])
    except (KeyError, TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(actual, expected)


def normalize_script(script):
    if not isinstance(script, list) or len(script) > 5000:
        raise ValueError("Script must be a list of at most 5,000 lines.")
    normalized = []
    seen = set()
    for line in script:
        if not isinstance(line, dict):
            raise ValueError("Each script line must be an object.")
        line_id = str(line.get("id", ""))[:80]
        text = line.get("text", "")
        roles = line.get("roles", [])
        comments = line.get("comments", [])
        if not line_id or line_id in seen:
            raise ValueError("Every script line needs a unique ID.")
        if not isinstance(text, str) or len(text) > 5000:
            raise ValueError("Line text must be 5,000 characters or fewer.")
        if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
            raise ValueError("Line roles must be a list of role names.")
        if not isinstance(comments, list) or len(comments) > 200:
            raise ValueError("A line can have at most 200 comments.")
        normalized_comments = []
        for comment in comments:
            if not isinstance(comment, dict):
                raise ValueError("Each comment must be an object.")
            comment_text = comment.get("text", "")
            comment_roles = comment.get("roles", [])
            if not isinstance(comment_text, str) or not comment_text.strip() or len(comment_text) > 2000:
                raise ValueError("Comments must be between 1 and 2,000 characters.")
            if not isinstance(comment_roles, list) or not all(
                isinstance(role, str) for role in comment_roles
            ):
                raise ValueError("Comment roles must be a list of role names.")
            normalized_comments.append({
                "id": str(comment.get("id") or uuid.uuid4().hex),
                "text": comment_text.strip(),
                "roles": comment_roles,
                "author": str(comment.get("author", ""))[:80],
            })
        seen.add(line_id)
        normalized.append({
            "id": line_id,
            "text": text,
            "roles": roles,
            "important": bool(line.get("important", False)),
            "comments": normalized_comments,
        })
    return normalized


def load_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        with STATE_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("users"), list):
            raise RuntimeError("The saved data file is invalid.")
        loaded.setdefault("roles", DEFAULT_ROLES.copy())
        loaded.setdefault("script", [])
        loaded.setdefault("playhead", {"lineId": None, "sequence": 0})
        return loaded

    password = os.environ.get("ROOT_PASSWORD", "")
    if len(password) < 12:
        raise RuntimeError(
            "Set ROOT_PASSWORD to a password of at least 12 characters before first run."
        )
    username = os.environ.get("ROOT_USERNAME", "root").strip()
    if not username:
        raise RuntimeError("ROOT_USERNAME cannot be empty.")
    return {
        "users": [{
            "username": username,
            "password": hash_password(password),
            "roles": DEFAULT_ROLES.copy(),
            "admin": True,
        }],
        "roles": DEFAULT_ROLES.copy(),
        "script": [],
        "playhead": {"lineId": None, "sequence": 0},
    }


state = load_state()


def save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temporary, STATE_FILE)


if not STATE_FILE.exists():
    save_state()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ScriptTeleprompter/1.0"

    def log_message(self, _format, *_args):
        pass

    def send_json(self, status, value, headers=None):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1_000_000:
                raise ValueError("Request body must be between 1 byte and 1 MB.")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object.")
            return payload
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Request body must contain valid JSON.")

    def get_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if not morsel:
            return None
        token = morsel.value
        session = sessions.get(token)
        if not session or session["expires"] < time.time():
            sessions.pop(token, None)
            return None
        with state_lock:
            return next(
                (user for user in state["users"] if user["username"] == session["username"]),
                None,
            )

    def require_user(self):
        user = self.get_user()
        if not user:
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Please sign in."})
        return user

    def require_admin(self):
        user = self.require_user()
        if user and not user.get("admin"):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Administrator access required."})
            return None
        return user

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/me":
            user = self.get_user()
            self.send_json(HTTPStatus.OK, {
                "user": self.public_user(user) if user else None,
                "roles": state["roles"],
            })
            return
        if path == "/api/state":
            user = self.require_user()
            if not user:
                return
            with state_lock:
                self.send_json(HTTPStatus.OK, {
                    "script": state["script"],
                    "roles": state["roles"],
                    "playhead": state["playhead"],
                    "users": [
                        self.public_user(item) for item in state["users"]
                    ] if user.get("admin") else [],
                })
            return
        if path == "/":
            self.serve_file("index.html", "text/html; charset=utf-8")
            return
        if path in ("/app.js", "/styles.css"):
            content_type = "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8"
            self.serve_file(path[1:], content_type)
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def serve_file(self, name, content_type):
        body = (ROOT / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def public_user(user):
        return {
            "username": user["username"],
            "roles": user.get("roles", []),
            "admin": bool(user.get("admin")),
        }

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/login":
                self.login(payload)
            elif path == "/api/logout":
                self.logout()
            elif path == "/api/account/password":
                self.change_password(payload)
            elif path == "/api/account/username":
                self.change_username(payload)
            elif path == "/api/users":
                self.add_user(payload)
            elif path == "/api/roles":
                self.add_role(payload)
            elif path == "/api/scroll":
                self.scroll(payload)
            elif path == "/api/comments":
                self.add_comment(payload)
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OSError:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not save data."})

    def login(self, payload):
        username = payload.get("username", "")
        password = payload.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "Enter a username and password."})
            return
        with state_lock:
            user = next(
                (item for item in state["users"] if item["username"].casefold() == username.strip().casefold()),
                None,
            )
            if not user or not verify_password(password, user["password"]):
                self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "Incorrect username or password."})
                return
            token = secrets.token_urlsafe(32)
            sessions[token] = {
                "username": user["username"],
                "expires": time.time() + SESSION_TTL,
            }
        self.send_json(HTTPStatus.OK, {"user": self.public_user(user)}, {
            "Set-Cookie": f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}",
        })

    def logout(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(SESSION_COOKIE)
        if morsel:
            sessions.pop(morsel.value, None)
        self.send_json(HTTPStatus.OK, {"ok": True}, {
            "Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
        })

    def change_password(self, payload):
        user = self.require_user()
        if not user:
            return
        old_password = payload.get("oldPassword", "")
        new_password = payload.get("newPassword", "")
        if not isinstance(old_password, str) or not verify_password(old_password, user["password"]):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "Current password is incorrect."})
            return
        if not isinstance(new_password, str) or len(new_password) < 12:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "New passwords must be at least 12 characters."})
            return
        with state_lock:
            user["password"] = hash_password(new_password)
            save_state()
        self.send_json(HTTPStatus.OK, {"ok": True})

    def change_username(self, payload):
        user = self.require_user()
        if not user:
            return
        username = payload.get("username", "")
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 40:
            raise ValueError("Username must be between 1 and 40 characters.")
        new_username = username.strip()
        with state_lock:
            if any(
                item is not user and item["username"].casefold() == new_username.casefold()
                for item in state["users"]
            ):
                raise ValueError("That username already exists.")
            old_username = user["username"]
            user["username"] = new_username
            for session in sessions.values():
                if session["username"] == old_username:
                    session["username"] = new_username
            save_state()
        self.send_json(HTTPStatus.OK, {"user": self.public_user(user)})

    def add_user(self, payload):
        admin = self.require_admin()
        if not admin:
            return
        username = payload.get("username", "")
        password = payload.get("password", "")
        roles = payload.get("roles", [])
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 40:
            raise ValueError("Username must be between 1 and 40 characters.")
        if not isinstance(password, str) or len(password) < 12:
            raise ValueError("Passwords must be at least 12 characters.")
        if not isinstance(roles, list) or not all(isinstance(role, str) and role in state["roles"] for role in roles):
            raise ValueError("Choose valid roles for this user.")
        with state_lock:
            if any(user["username"].casefold() == username.strip().casefold() for user in state["users"]):
                raise ValueError("That username already exists.")
            state["users"].append({
                "username": username.strip(),
                "password": hash_password(password),
                "roles": roles,
                "admin": False,
            })
            save_state()
        self.send_json(HTTPStatus.CREATED, {"ok": True})

    def add_role(self, payload):
        if not self.require_admin():
            return
        role = payload.get("role", "")
        if not isinstance(role, str) or not role.strip() or len(role.strip()) > 40:
            raise ValueError("Role names must be between 1 and 40 characters.")
        with state_lock:
            if any(item.casefold() == role.strip().casefold() for item in state["roles"]):
                raise ValueError("That role already exists.")
            state["roles"].append(role.strip())
            save_state()
        self.send_json(HTTPStatus.CREATED, {"ok": True})

    def scroll(self, payload):
        if not self.require_user():
            return
        line_id = payload.get("lineId")
        with state_lock:
            if line_id is not None and not any(line["id"] == line_id for line in state["script"]):
                raise ValueError("That script line no longer exists.")
            state["playhead"] = {
                "lineId": line_id,
                "sequence": state["playhead"]["sequence"] + 1,
            }
            save_state()
        self.send_json(HTTPStatus.OK, {"playhead": state["playhead"]})

    def add_comment(self, payload):
        user = self.require_user()
        if not user:
            return
        line_id = payload.get("lineId")
        text = payload.get("text", "")
        roles = payload.get("roles", [])
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("Comments must be between 1 and 2,000 characters.")
        if not isinstance(roles, list) or not all(isinstance(role, str) and role in state["roles"] for role in roles):
            raise ValueError("Choose valid roles for this comment.")
        with state_lock:
            line = next((item for item in state["script"] if item["id"] == line_id), None)
            if not line:
                raise ValueError("That script line no longer exists.")
            line["comments"].append({
                "id": uuid.uuid4().hex,
                "text": text.strip(),
                "roles": roles,
                "author": user["username"],
            })
            save_state()
        self.send_json(HTTPStatus.CREATED, {"ok": True})

    def do_PUT(self):
        if urlparse(self.path).path != "/api/script":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        if not self.require_admin():
            return
        try:
            payload = self.read_json()
            script = normalize_script(payload.get("script"))
            with state_lock:
                role_set = set(state["roles"])
                if any(
                    role not in role_set
                    for line in script
                    for role in line["roles"] + [
                        assigned for comment in line["comments"] for assigned in comment["roles"]
                    ]
                ):
                    raise ValueError("Script lines and comments can only use existing roles.")
                state["script"] = script
                if state["playhead"]["lineId"] and not any(
                    line["id"] == state["playhead"]["lineId"] for line in script
                ):
                    state["playhead"] = {
                        "lineId": script[0]["id"] if script else None,
                        "sequence": state["playhead"]["sequence"] + 1,
                    }
                save_state()
            self.send_json(HTTPStatus.OK, {"ok": True})
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except OSError:
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not save data."})


def local_addresses():
    addresses = set()
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = result[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    return sorted(addresses)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    print(f"Script teleprompter listening at http://localhost:{port}")
    for address in local_addresses():
        print(f"On your network: http://{address}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
        server.server_close()
