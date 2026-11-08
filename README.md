# Script Cue

A lightweight, self-hosted script and teleprompter for productions. Script lines have role visibility, role-targeted comments, and important-cue alerts. Arrow-key cue changes are shared with everyone signed in; wheel, trackpad, touch, and scrollbar navigation stay local until someone jumps back to the live cue.

## Run it

Requires Python 3.10 or later; there are no third-party packages.

On first run, configure the initial administrator using environment variables. Passwords are never stored as plaintext; the server stores salted PBKDF2 password hashes in `data/state.json`.

PowerShell:

```powershell
$env:ROOT_USERNAME = "root"
$env:ROOT_PASSWORD = "choose-a-unique-password-of-12-or-more-characters"
python server.py
```

macOS/Linux:

```sh
ROOT_USERNAME=root ROOT_PASSWORD='choose-a-unique-password-of-12-or-more-characters' python3 server.py
```

By default, the server listens on port `8080` on all network interfaces and prints its available LAN addresses. Open the printed `http://<server-ip>:8080` address on other devices on the same network. Set `PORT` or `HOST` to override the defaults. Make sure the host firewall allows the chosen port. This built-in server is intended for a trusted private network; use a TLS reverse proxy and network access controls before exposing it to the public internet.

The initial root account is created only when the data file does not exist. To change the password later, sign in and use **Settings → Change password**. The administrator can add user accounts and production roles from the sidebar. User passwords are not recoverable; an administrator should create a replacement account if a user loses their password.

To move the production to another host, stop the server and copy `data/state.json` securely. Do not commit that file: it contains account password hashes and production content.

## Use the teleprompter

- Sign in as an administrator and use **Choose text file…** to open your computer's file picker and choose a `.txt` script. In a supported browser on a secure origin, changes made in Script Cue are also written back to that selected file. To edit a file on the server computer directly, open the app there using `http://localhost:8080` in a supported browser. If direct file access is unavailable, the chosen file is uploaded and changes update the server-side copy instead. Use **Browse server** to select a file on the machine running the app, or **New .txt** to create a script there. The selected file is the live script for everyone.
- Script files are plain text: each ordinary line is a cue, and a role-targeted note goes on the next line in the format `N (note text)role1,role2`. For example:

  ```text
  will: hi
  N (lights up)lights,director
  Next line
  ```

  The note belongs to the cue immediately above it and is not a cue itself. Role names in note lines are comma-separated; roles found in notes are added to the production's role list. Adding or editing cues and notes in Script Cue updates the selected file (or its server-side copy when direct browser file access is unavailable). Keep each cue and note on one physical line.
- **Browse server** operates on the computer running `server.py`; **Choose text file…** opens the browser device's picker. Treat administrator access as trusted and back up files before editing them in Script Cue.
- Lines can be reordered with the up/down controls.
- Select roles on a line to mark its intended audience. Each person independently chooses which roles to display.
- Add comments to a line and select the roles the note applies to. Mark important cues to show a warning when a visible line is brought live.
- Press the up/down arrow keys to advance or rewind the shared live cue. Scrolling by touch, wheel, trackpad, or scrollbar moves only your own view. Use **Jump back to live cue** to resync.
- Change theme, font, and text size in **Settings**; display choices are saved in that browser.
