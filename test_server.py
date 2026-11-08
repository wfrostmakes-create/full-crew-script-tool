import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from unittest import mock


_test_data = tempfile.TemporaryDirectory()
os.environ["SCRIPT_TOOL_DATA_DIR"] = _test_data.name
import server


class ScriptFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drive = tempfile.TemporaryDirectory()
        server.FILE_ROOT = Path(cls.drive.name).resolve()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.RequestHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()
        cls.drive.cleanup()
        _test_data.cleanup()

    def setUp(self):
        with server.state_lock:
            server.state["roles"] = ["Actor", "Director", "lights"]
            server.state["script"] = []
            server.state["selected_file"] = ""
            server.state["selected_name"] = ""
            server.state["playhead"] = {"lineId": None, "sequence": 0}
            server.state["users"] = [{
                "username": "root",
                "password": server.hash_password("test-password-long-enough"),
                "roles": ["Actor", "Director", "lights"],
                "admin": True,
            }]
            server.sessions.clear()
            server.save_state()
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request_json("/api/login", "POST", {
            "username": "root",
            "password": "test-password-long-enough",
        })

    def request_json(self, path, method="GET", payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(request) as response:
            return response.status, json.loads(response.read())

    def test_text_format_round_trip(self):
        contents = "will: hi\nN (lights up)lights,Director\nNext cue"
        script = server.parse_text_script(contents)
        self.assertEqual(script[0]["text"], "will: hi")
        self.assertEqual(script[0]["comments"][0]["text"], "lights up")
        self.assertEqual(script[0]["comments"][0]["roles"], ["lights", "Director"])
        self.assertEqual(server.serialize_text_script(script), contents)

    def test_first_run_uses_root_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            with mock.patch.object(server, "DATA_DIR", data_dir), \
                    mock.patch.object(server, "STATE_FILE", data_dir / "state.json"):
                initial = server.load_state()
        admin = initial["users"][0]
        self.assertEqual(admin["username"], "root")
        self.assertTrue(server.verify_password("root", admin["password"]))

    def test_admin_can_browse_open_and_create_text_files(self):
        show_folder = server.FILE_ROOT / "Shows"
        show_folder.mkdir()
        (show_folder / "rehearsal.txt").write_text(
            "Opening line\nN (lights up)lights\n",
            encoding="utf-8",
        )
        status, listing = self.request_json("/api/script-files?path=Shows")
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in listing["entries"]], ["rehearsal.txt"])

        status, created = self.request_json("/api/script-files/create", "POST", {
            "directory": "Shows",
            "name": "new-show",
        })
        self.assertEqual(status, 201)
        self.assertEqual(created["selectedFile"], "Shows/new-show.txt")
        self.assertTrue((show_folder / "new-show.txt").exists())

        self.request_json("/api/script-files/open", "POST", {"path": "Shows/rehearsal.txt"})
        _, state = self.request_json("/api/state")
        self.assertEqual(state["selectedFile"], "Shows/rehearsal.txt")
        self.assertEqual(state["script"][0]["comments"][0]["text"], "lights up")
        self.assertIn("lights", state["roles"])

    def test_reload_imports_server_file_edits_and_preserves_matching_cue_metadata(self):
        path = server.FILE_ROOT / "scene.txt"
        path.write_text("First cue\nSecond cue", encoding="utf-8")
        self.request_json("/api/script-files/open", "POST", {"path": "scene.txt"})
        _, state = self.request_json("/api/state")
        first_line = state["script"][0]
        previous_sequence = state["playhead"]["sequence"]
        first_line["roles"] = ["Actor"]
        first_line["important"] = True
        self.request_json("/api/script", "PUT", {
            "selectedFile": "scene.txt",
            "script": state["script"],
        })

        path.write_text("Added cue\nFirst cue\nSecond cue\nN (lights up)lights", encoding="utf-8")
        status, result = self.request_json("/api/script-files/reload", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(result["reloaded"])
        _, state = self.request_json("/api/state")
        self.assertEqual([line["text"] for line in state["script"]], [
            "Added cue", "First cue", "Second cue",
        ])
        self.assertEqual(state["script"][1]["roles"], ["Actor"])
        self.assertTrue(state["script"][1]["important"])
        self.assertEqual(state["script"][2]["comments"][0]["text"], "lights up")
        self.assertEqual(state["playhead"]["sequence"], previous_sequence)

    def test_reload_imports_edits_to_uploaded_server_copy(self):
        _, opened = self.request_json("/api/script-files/upload", "POST", {
            "name": "local-script.txt",
            "contents": "Original cue",
        })
        path = server.resolve_selected_script_path(opened["selectedFile"])
        path.write_text("Updated cue\nNext cue", encoding="utf-8")

        _, result = self.request_json("/api/script-files/reload", "POST", {})
        self.assertTrue(result["reloaded"])
        _, state = self.request_json("/api/state")
        self.assertEqual(
            [line["text"] for line in state["script"]],
            ["Updated cue", "Next cue"],
        )
    def test_admin_can_open_empty_text_file_with_spaces_in_path(self):
        show_folder = server.FILE_ROOT / "church-show-files"
        show_folder.mkdir()
        (show_folder / "test script.txt").write_text("", encoding="utf-8")

        self.request_json("/api/script-files/open", "POST", {
            "path": "church-show-files/test script.txt",
        })
        _, state = self.request_json("/api/state")

        self.assertEqual(state["selectedFile"], "church-show-files/test script.txt")
        self.assertEqual(state["script"], [])

    def test_adding_note_updates_selected_plain_text_file(self):
        path = server.FILE_ROOT / "scene.txt"
        path.write_text("Cue line", encoding="utf-8")
        self.request_json("/api/script-files/open", "POST", {"path": "scene.txt"})
        _, state = self.request_json("/api/state")
        self.request_json("/api/comments", "POST", {
            "lineId": state["script"][0]["id"],
            "text": "lights up",
            "roles": ["lights", "Director"],
        })
        self.assertEqual(path.read_text(encoding="utf-8"), "Cue line\nN (lights up)lights,Director")

    def test_saving_cue_edits_updates_selected_plain_text_file(self):
        path = server.FILE_ROOT / "scene.txt"
        path.write_text("Old cue", encoding="utf-8")
        self.request_json("/api/script-files/open", "POST", {"path": "scene.txt"})
        _, state = self.request_json("/api/state")
        line = state["script"][0]
        line["text"] = "Updated cue"
        self.request_json("/api/script", "PUT", {
            "selectedFile": "scene.txt",
            "script": [line],
        })
        self.assertEqual(path.read_text(encoding="utf-8"), "Updated cue")

    def test_browser_file_upload_imports_plain_text_script(self):
        status, result = self.request_json("/api/script-files/upload", "POST", {
            "name": "picked-script.txt",
            "contents": "First cue\nN (lights up)lights\nSecond cue",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["selectedName"], "picked-script.txt")
        _, state = self.request_json("/api/state")
        self.assertEqual(state["script"][0]["comments"][0]["text"], "lights up")
        imported = server.resolve_selected_script_path(state["selectedFile"])
        self.assertEqual(
            imported.read_text(encoding="utf-8"),
            "First cue\nN (lights up)lights\nSecond cue",
        )

    def test_upload_rejects_non_text_files(self):
        with self.assertRaises(HTTPError) as error:
            self.request_json("/api/script-files/upload", "POST", {
                "name": "script.doc",
                "contents": "Not a text script",
            })
        self.assertEqual(error.exception.code, 400)

    def test_saving_uploaded_script_updates_server_copy(self):
        _, opened = self.request_json("/api/script-files/upload", "POST", {
            "name": "local-script.txt",
            "contents": "Original cue",
        })
        _, state = self.request_json("/api/state")
        line = state["script"][0]
        line["text"] = "Edited cue"
        self.request_json("/api/script", "PUT", {
            "selectedFile": opened["selectedFile"],
            "script": [line],
        })
        path = server.resolve_selected_script_path(opened["selectedFile"])
        self.assertEqual(path.read_text(encoding="utf-8"), "Edited cue")

    def test_browser_rejects_paths_outside_system_drive(self):
        request = Request(self.base_url + "/api/script-files?" + urlencode({"path": "../.."}))
        with self.assertRaises(HTTPError) as error:
            self.opener.open(request)
        self.assertEqual(error.exception.code, 400)

    def test_only_admins_can_browse_script_files(self):
        selected_path = server.FILE_ROOT / "viewer-reload.txt"
        selected_path.write_text("Before reload", encoding="utf-8")
        self.request_json("/api/script-files/open", "POST", {"path": "viewer-reload.txt"})
        self.request_json("/api/users", "POST", {
            "username": "viewer",
            "password": "viewer-password-long-enough",
            "roles": ["Actor"],
        })
        viewer_opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        login = Request(
            self.base_url + "/api/login",
            data=json.dumps({
                "username": "viewer",
                "password": "viewer-password-long-enough",
            }).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        viewer_opener.open(login).close()
        with self.assertRaises(HTTPError) as error:
            viewer_opener.open(self.base_url + "/api/script-files")
        self.assertEqual(error.exception.code, 403)

        selected_path.write_text("After reload", encoding="utf-8")
        reload_request = Request(
            self.base_url + "/api/script-files/reload",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        viewer_opener.open(reload_request).close()
        with viewer_opener.open(self.base_url + "/api/state") as response:
            state = json.loads(response.read())
        self.assertEqual(state["script"][0]["text"], "After reload")

    def test_short_passwords_are_accepted_but_empty_passwords_are_rejected(self):
        self.request_json("/api/users", "POST", {
            "username": "short-password-user",
            "password": "x",
            "roles": ["Actor"],
        })

        short_password_opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        login = Request(
            self.base_url + "/api/login",
            data=json.dumps({
                "username": "short-password-user",
                "password": "x",
            }).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        short_password_opener.open(login).close()

        with self.assertRaises(HTTPError) as error:
            self.request_json("/api/users", "POST", {
                "username": "empty-password-user",
                "password": "",
                "roles": ["Actor"],
            })
        self.assertEqual(error.exception.code, 400)

        self.request_json("/api/account/password", "POST", {
            "oldPassword": "test-password-long-enough",
            "newPassword": "r",
        })
        replacement_opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        replacement_login = Request(
            self.base_url + "/api/login",
            data=json.dumps({"username": "root", "password": "r"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        replacement_opener.open(replacement_login).close()


if __name__ == "__main__":
    unittest.main()
