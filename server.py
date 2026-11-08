import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).parent
DATA_DIR = Path(os.environ.get("SCRIPT_TOOL_DATA_DIR", ROOT / "data"))
STATE_FILE = DATA_DIR / "state.json"
UPLOAD_DIR = DATA_DIR / "scripts"
SESSION_COOKIE = "script_session"
SESSION_TTL = 60 * 60 * 24 * 7
PASSWORD_ITERATIONS = 310_000
DEFAULT_ROLES = ["Actor", "Director", "Stage Manager", "Crew"]
FILE_ROOT = Path(os.environ.get("SystemDrive", Path(__file__).anchor) + os.sep).resolve()
NOTE_LINE = re.compile(r"^N \((.*)\)(.*)$")
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


def resolve_script_path(relative_path, allow_directory=False):
    if not isinstance(relative_path, str) or "\0" in relative_path:
        raise ValueError("Choose a valid path on the server's system drive.")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        raise ValueError("Paths must be relative to the server's system drive.")
    try:
        resolved = (FILE_ROOT / candidate).resolve()
    except (OSError, RuntimeError) as error:
        raise ValueError("The selected path cannot be accessed.") from error
    if not resolved.is_relative_to(FILE_ROOT):
        raise ValueError("The selected path must stay on the server's system drive.")
    if allow_directory and not resolved.is_dir():
        raise ValueError("That folder does not exist or cannot be accessed.")
    if not allow_directory and (resolved.suffix.casefold() != ".txt" or not resolved.is_file()):
        raise ValueError("Choose an existing .txt script file.")
    return resolved


def resolve_selected_script_path(relative_path):
    if isinstance(relative_path, str) and relative_path.startswith("@upload/"):
        filename = relative_path.removeprefix("@upload/")
        if not re.fullmatch(r"[0-9a-f]{32}\.txt", filename):
            raise ValueError("The selected uploaded script is invalid.")
        upload_root = UPLOAD_DIR.resolve()
        if not upload_root.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("The uploaded script folder is not available.")
        path = (upload_root / filename).resolve()
        if not path.is_relative_to(upload_root) or not path.is_file():
            raise ValueError("The selected uploaded script no longer exists.")
        return path
    return resolve_script_path(relative_path)


def relative_script_path(path):
    relative = path.relative_to(FILE_ROOT).as_posix()
    return "" if relative == "." else relative


def parse_text_script(contents):
    if len(contents) > 1_000_000:
        raise ValueError("Script files must be 1 MB or smaller.")
    script = []
    line_count = 0
    for text in contents.splitlines():
        note_match = NOTE_LINE.fullmatch(text)
        if note_match:
            if not script:
                raise ValueError("A note line must follow a script line.")
            note_text, roles_text = note_match.groups()
            roles = [role.strip() for role in roles_text.split(",") if role.strip()]
            if not note_text.strip() or len(note_text) > 2000 or len(roles) > 20 or any(
                len(role) > 40 or ")" in role for role in roles
            ):
                raise ValueError("Note lines must have valid note text and role names.")
            if len(script[-1]["comments"]) >= 200:
                raise ValueError("A script line can have at most 200 note lines.")
            script[-1]["comments"].append({
                "id": uuid.uuid4().hex,
                "text": note_text,
                "roles": roles,
                "author": "",
            })
        elif text.startswith("N ("):
            raise ValueError("A note line must use the format N (note text)role1,role2.")
        else:
            if len(text) > 5000:
                raise ValueError("Script lines must be 5,000 characters or fewer.")
            line_count += 1
            if line_count > 5000:
                raise ValueError("Script files can have at most 5,000 cue lines.")
            script.append({
                "id": uuid.uuid4().hex,
                "text": text,
                "roles": [],
                "important": False,
                "comments": [],
            })
    return script


def serialize_text_script(script):
    output = []
    for line in script:
        text = line["text"]
        if "\n" in text or "\r" in text:
            raise ValueError("Each script cue must fit on one line.")
        if text.startswith("N (") and NOTE_LINE.fullmatch(text):
            raise ValueError("Script cue lines cannot use the reserved note-line format.")
        output.append(text)
        for comment in line["comments"]:
            note_text = comment["text"]
            roles = comment["roles"]
            if "\n" in note_text or "\r" in note_text or any(
                not role or "," in role or "\n" in role or "\r" in role or ")" in role
                for role in roles
            ):
                raise ValueError("Notes and roles must fit the plain-text note format.")
            output.append(f"N ({note_text}){','.join(roles)}")
    contents = "\n".join(output)
    if len(contents) > 1_000_000:
        raise ValueError("Script files must be 1 MB or smaller.")
    return contents


def write_text_script(path, script):
    contents = serialize_text_script(script)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".script-cue-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


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
        loaded.setdefault("selected_file", "")
        loaded.setdefault("selected_name", "")
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
        "selected_file": "",
        "selected_name": "",
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
            if length < 1 or length > 2_000_000:
                raise ValueError("Request body must be between 1 byte and 2 MB.")
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
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/script-files":
            if not self.require_admin():
                return
            try:
                directory = resolve_script_path(
                    parse_qs(parsed.query).get("path", [""])[0],
                    allow_directory=True,
                )
                entries = []
                for item in directory.iterdir():
                    try:
                        resolved = item.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if not resolved.is_relative_to(FILE_ROOT):
                        continue
                    if resolved.is_dir():
                        entries.append({
                            "name": item.name,
                            "path": relative_script_path(resolved),
                            "directory": True,
                        })
                    elif resolved.is_file() and resolved.suffix.casefold() == ".txt":
                        entries.append({
                            "name": item.name,
                            "path": relative_script_path(resolved),
                            "directory": False,
                        })
                entries.sort(key=lambda item: (not item["directory"], item["name"].casefold()))
                self.send_json(HTTPStatus.OK, {
                    "path": relative_script_path(directory),
                    "parent": relative_script_path(directory.parent) if directory != FILE_ROOT else None,
                    "entries": entries,
                    "selectedFile": state.get("selected_file", ""),
                })
            except (OSError, ValueError) as error:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
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
                    "selectedFile": state.get("selected_file", ""),
                    "selectedName": state.get("selected_name", ""),
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
            elif path == "/api/script-files/open":
                self.open_script_file(payload)
            elif path == "/api/script-files/upload":
                self.upload_script_file(payload)
            elif path == "/api/script-files/create":
                self.create_script_file(payload)
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
        if (
            not isinstance(role, str)
            or not role.strip()
            or len(role.strip()) > 40
            or any(character in role for character in ",)\r\n")
        ):
            raise ValueError("Role names must be 1 to 40 characters and cannot contain commas or closing parentheses.")
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
            comment = {
                "id": uuid.uuid4().hex,
                "text": text.strip(),
                "roles": roles,
                "author": user["username"],
            }
            if state.get("selected_file"):
                path = resolve_selected_script_path(state["selected_file"])
                updated_script = [
                    {**item, "comments": item["comments"].copy()}
                    for item in state["script"]
                ]
                updated_line = next(item for item in updated_script if item["id"] == line_id)
                updated_line["comments"].append(comment)
                write_text_script(path, updated_script)
                line["comments"].append(comment)
            save_state()
        self.send_json(HTTPStatus.CREATED, {"ok": True})

    def open_script_file(self, payload):
        if not self.require_admin():
            return
        path = resolve_script_path(payload.get("path"))
        with path.open("r", encoding="utf-8-sig", newline=None) as file:
            script = parse_text_script(file.read())
        with state_lock:
            if state.get("selected_file") == relative_script_path(path):
                old_lines = state["script"]
                for index, line in enumerate(script):
                    if index >= len(old_lines) or line["text"] != old_lines[index]["text"]:
                        continue
                    previous = old_lines[index]
                    line["roles"] = previous.get("roles", [])
                    line["important"] = previous.get("important", False)
                    for comment in line["comments"]:
                        old_comment = next((
                            item for item in previous.get("comments", [])
                            if item["text"] == comment["text"] and item["roles"] == comment["roles"]
                        ), None)
                        if old_comment:
                            comment["author"] = old_comment.get("author", "")
                            comment["id"] = old_comment["id"]
            state["selected_file"] = relative_script_path(path)
            state["selected_name"] = path.name
            known_roles = {role.casefold(): role for role in state["roles"]}
            for line in script:
                for comment in line["comments"]:
                    normalized_roles = []
                    for role in comment["roles"]:
                        normalized_role = known_roles.get(role.casefold())
                        if normalized_role is None:
                            normalized_role = role
                            state["roles"].append(role)
                            known_roles[role.casefold()] = role
                        normalized_roles.append(normalized_role)
                    comment["roles"] = normalized_roles
            state["script"] = script
            state["playhead"] = {
                "lineId": script[0]["id"] if script else None,
                "sequence": state["playhead"]["sequence"] + 1,
            }
            save_state()
        self.send_json(HTTPStatus.OK, {
            "ok": True,
            "selectedFile": state["selected_file"],
            "selectedName": state["selected_name"],
            "script": state["script"],
            "playhead": state["playhead"],
        })

    def upload_script_file(self, payload):
        if not self.require_admin():
            return
        name = payload.get("name", "")
        contents = payload.get("contents", "")
        if (
            not isinstance(name, str)
            or not name.strip()
            or Path(name).name != name
            or Path(name).suffix.casefold() != ".txt"
        ):
            raise ValueError("Choose a .txt script file.")
        if not isinstance(contents, str) or len(contents.encode("utf-8")) > 1_000_000:
            raise ValueError("Script files must be 1 MB or smaller.")
        script = parse_text_script(contents)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        upload_root = UPLOAD_DIR.resolve()
        if not upload_root.is_relative_to(DATA_DIR.resolve()):
            raise ValueError("The uploaded script folder is not available.")
        filename = f"{uuid.uuid4().hex}.txt"
        path = upload_root / filename
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(serialize_text_script(script))
        with state_lock:
            state["selected_file"] = f"@upload/{filename}"
            state["selected_name"] = name.strip()
            known_roles = {role.casefold(): role for role in state["roles"]}
            for line in script:
                for comment in line["comments"]:
                    normalized_roles = []
                    for role in comment["roles"]:
                        normalized_role = known_roles.get(role.casefold())
                        if normalized_role is None:
                            normalized_role = role
                            state["roles"].append(role)
                            known_roles[role.casefold()] = role
                        normalized_roles.append(normalized_role)
                    comment["roles"] = normalized_roles
            state["script"] = script
            state["playhead"] = {
                "lineId": script[0]["id"] if script else None,
                "sequence": state["playhead"]["sequence"] + 1,
            }
            save_state()
        self.send_json(HTTPStatus.OK, {
            "ok": True,
            "selectedFile": state["selected_file"],
            "selectedName": state["selected_name"],
            "script": state["script"],
            "playhead": state["playhead"],
        })

    def create_script_file(self, payload):
        if not self.require_admin():
            return
        directory = resolve_script_path(payload.get("directory", ""), allow_directory=True)
        name = payload.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Enter a name for the new script.")
        name = name.strip()
        if (
            Path(name).name != name
            or name in (".", "..")
            or any(character in name for character in '\\/:*?"<>|\r\n\0')
        ):
            raise ValueError("Enter a file name, not a folder path.")
        if Path(name).suffix.casefold() != ".txt":
            name += ".txt"
        path = (directory / name).resolve()
        if not path.is_relative_to(FILE_ROOT) or path.suffix.casefold() != ".txt":
            raise ValueError("Choose a .txt file on the server's system drive.")
        if path.exists():
            raise ValueError("A file with that name already exists.")
        path.parent.mkdir(parents=False, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write("")
        with state_lock:
            state["selected_file"] = relative_script_path(path)
            state["selected_name"] = path.name
            state["script"] = []
            state["playhead"] = {
                "lineId": None,
                "sequence": state["playhead"]["sequence"] + 1,
            }
            save_state()
        self.send_json(HTTPStatus.CREATED, {
            "ok": True,
            "selectedFile": state["selected_file"],
            "selectedName": state["selected_name"],
            "script": [],
            "playhead": state["playhead"],
        })

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
                if state.get("selected_file") and payload.get("selectedFile") != state["selected_file"]:
                    raise ValueError("The live script file changed. Reopen it before saving your edits.")
                if any(
                    role not in role_set
                    for line in script
                    for role in line["roles"] + [
                        assigned for comment in line["comments"] for assigned in comment["roles"]
                    ]
                ):
                    raise ValueError("Script lines and comments can only use existing roles.")
                if state.get("selected_file"):
                    write_text_script(resolve_selected_script_path(state["selected_file"]), script)
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
