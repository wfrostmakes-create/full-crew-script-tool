const $ = (selector, root = document) => root.querySelector(selector);
const elements = {
  app: $("#app"),
  login: $("#login-view"),
  loginForm: $("#login-form"),
  loginError: $("#login-error"),
  scriptList: $("#script-list"),
  roleFilters: $("#role-filters"),
  lineCount: $("#line-count"),
  playhead: null,
};

let currentUser = null;
let currentState = { script: [], roles: [], playhead: { lineId: null, sequence: 0 }, users: [], selectedFile: "", selectedName: "" };
let selectedRoles = new Set();
let followingLive = true;
let dirty = false;
let pollTimer;
let toastTimer;
let lastPlayheadSequence = 0;
let programmaticScroll = false;
let renderedRoleSignature = "";
let browserDirectory = "";
let selectedFileHandle = null;

function roleStorageKey() {
  return `script-cue-roles:${currentUser?.username || "default"}`;
}

async function request(path, options = {}) {
  const apiPath = path.replace(/^\/+/, "");
  const response = await fetch(new URL(apiPath, document.baseURI), {
    credentials: "same-origin",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const result = response.status === 204 ? {} : await response.json();
  if (!response.ok) throw new Error(result.error || "Something went wrong.");
  return result;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("visible"), 2600);
}

function showLogin() {
  clearInterval(pollTimer);
  selectedFileHandle = null;
  elements.app.hidden = true;
  elements.login.hidden = false;
}

function showApp(user) {
  currentUser = user;
  const savedRoles = localStorage.getItem(roleStorageKey());
  if (savedRoles) {
    selectedRoles = new Set(JSON.parse(savedRoles));
  } else {
    selectedRoles = new Set(user.admin ? currentState.roles : user.roles);
  }
  elements.login.hidden = true;
  elements.app.hidden = false;
  $("#user-name").textContent = user.username;
  $("#admin-panel").hidden = !user.admin;
  $("#script-file-admin-actions").hidden = !user.admin;
  $("#add-line").hidden = !user.admin || !currentState.selectedFile;
  $("#empty-add-line").hidden = !user.admin || !currentState.selectedFile;
  $("#sync-status").textContent = "Connected";
  applyPreferences();
  fetchState(true);
  clearInterval(pollTimer);
  pollTimer = setInterval(() => fetchState(false), 1000);
}

async function fetchState(initial) {
  try {
    const previousSequence = lastPlayheadSequence;
    const data = await request("/api/state");
    const changedScript = JSON.stringify(data.script) !== JSON.stringify(currentState.script);
    const changedFile = data.selectedFile !== currentState.selectedFile;
    const localScript = currentState.script;
    const localFile = currentState.selectedFile;
    const localName = currentState.selectedName;
    if (changedFile) selectedFileHandle = null;
    currentState = data;
    if (dirty) {
      currentState.script = localScript;
      currentState.selectedFile = localFile;
      currentState.selectedName = localName;
      const serverLines = new Map(data.script.map(line => [line.id, line]));
      currentState.script.forEach(line => {
        const savedLine = serverLines.get(line.id);
        if (savedLine) line.comments = savedLine.comments;
      });
    } else if (changedScript) {
      currentState.script = data.script;
    }
    renderRoleFilters();
    if (!dirty && (initial || changedScript || changedFile)) renderScript();
    renderSelectedFile();
    $("#add-line").hidden = !currentUser.admin || !currentState.selectedFile;
    $("#empty-add-line").hidden = !currentUser.admin || !currentState.selectedFile;
    if (data.playhead.sequence !== previousSequence && !initial) {
      lastPlayheadSequence = data.playhead.sequence;
      if (followingLive) {
        goToLine(data.playhead.lineId, true);
      } else if (!dirty) {
        renderScript();
      }
    } else {
      lastPlayheadSequence = data.playhead.sequence;
    }
    $("#sync-status").textContent = "Connected";
  } catch (error) {
    $("#sync-status").textContent = "Reconnecting…";
  }
}

function isLineVisible(line) {
  return line.roles.length === 0 || line.roles.some(role => selectedRoles.has(role));
}

function visibleComments(line) {
  return (line.comments || []).filter(comment =>
    comment.roles.length === 0 || comment.roles.some(role => selectedRoles.has(role))
  );
}

function create(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function roleCheckboxes(container, roles, selected, onChange, className = "") {
  container.replaceChildren();
  roles.forEach(role => {
    const label = create("label", className || "checkbox-item");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected.has(role);
    checkbox.addEventListener("change", () => onChange(role, checkbox.checked));
    label.append(checkbox, document.createTextNode(role));
    container.append(label);
  });
}

function renderRoleFilters() {
  const { roles } = currentState;
  if (!selectedRoles.size && !localStorage.getItem(roleStorageKey())) {
    selectedRoles = new Set(currentUser.admin ? roles : currentUser.roles);
  }
  selectedRoles = new Set([...selectedRoles].filter(role => roles.includes(role)));
  roleCheckboxes(elements.roleFilters, roles, selectedRoles, (role, checked) => {
    checked ? selectedRoles.add(role) : selectedRoles.delete(role);
    localStorage.setItem(roleStorageKey(), JSON.stringify([...selectedRoles]));
    renderRoleFilters();
    if (!dirty) renderScript();
  });
  const userRoleContainer = $("#new-user-roles");
  const roleSignature = JSON.stringify(roles);
  if (userRoleContainer && currentUser?.admin && roleSignature !== renderedRoleSignature) {
    roleCheckboxes(userRoleContainer, roles, new Set(), () => {}, "checkbox-item");
    renderedRoleSignature = roleSignature;
  }
}

function renderScript() {
  const script = currentState.script;
  const visible = script.filter(isLineVisible);
  elements.lineCount.textContent = `${visible.length} ${visible.length === 1 ? "line" : "lines"}`;
  $("#script-empty").hidden = visible.length > 0;
  $("#empty-copy").textContent = script.length
    ? "No lines match the roles you selected. Try another role in the sidebar."
    : currentState.selectedFile
      ? "There are no script lines yet."
      : "Choose or create a .txt script file to get started.";
  $("#save-script")?.remove();
  if (currentUser.admin && dirty) {
    const saveButton = create("button", "primary save-script", "Save changes");
    saveButton.id = "save-script";
    saveButton.addEventListener("click", saveScript);
    $(".script-toolbar").append(saveButton);
  }

  elements.scriptList.replaceChildren();
  visible.forEach(line => {
    const card = create("article", `script-card${line.important ? " important-line" : ""}`);
    card.dataset.lineId = line.id;
    if (line.id === currentState.playhead.lineId) card.classList.add("live-line");
    const lineHead = create("div", "line-head");
    const cueNumber = create("span", "cue-number", String(script.indexOf(line) + 1).padStart(2, "0"));
    const tags = create("div", "line-tags");
    line.roles.forEach(role => tags.append(create("span", "role-tag", role)));
    if (line.important) tags.append(create("span", "important-tag", "Important cue"));
    lineHead.append(cueNumber, tags);
    card.append(lineHead);

    if (currentUser.admin) {
      const editor = create("textarea", "line-editor");
      editor.setAttribute("aria-label", `Script line ${script.indexOf(line) + 1}`);
      editor.rows = Math.max(2, Math.min(7, Math.ceil(line.text.length / 72)));
      editor.value = line.text;
      editor.placeholder = "Write this line…";
      editor.addEventListener("input", () => {
        line.text = editor.value;
        dirty = true;
        updateSaveButton();
      });
      card.append(editor);
      const lineOptions = create("div", "line-options");
      const rolesBox = create("div", "line-role-options");
      roleCheckboxes(rolesBox, currentState.roles, new Set(line.roles), (role, checked) => {
        line.roles = checked ? [...new Set([...line.roles, role])] : line.roles.filter(item => item !== role);
        dirty = true;
        updateSaveButton();
      }, "small-checkbox");
      lineOptions.append(rolesBox);
      const importantLabel = create("label", "small-checkbox important-check");
      const importantInput = document.createElement("input");
      importantInput.type = "checkbox";
      importantInput.checked = line.important;
      importantInput.addEventListener("change", () => {
        line.important = importantInput.checked;
        dirty = true;
        updateSaveButton();
        renderScript();
      });
      importantLabel.append(importantInput, document.createTextNode("Important"));
      lineOptions.append(importantLabel);
      const moveUp = create("button", "move-button", "↑");
      moveUp.type = "button";
      moveUp.title = "Move line up";
      moveUp.disabled = script.indexOf(line) === 0;
      moveUp.addEventListener("click", () => moveLine(line.id, -1));
      const moveDown = create("button", "move-button", "↓");
      moveDown.type = "button";
      moveDown.title = "Move line down";
      moveDown.disabled = script.indexOf(line) === script.length - 1;
      moveDown.addEventListener("click", () => moveLine(line.id, 1));
      const remove = create("button", "move-button delete-button", "×");
      remove.type = "button";
      remove.title = "Delete line";
      remove.addEventListener("click", () => {
        if (confirm("Delete this script line and its comments?")) {
          currentState.script = currentState.script.filter(item => item.id !== line.id);
          dirty = true;
          renderScript();
        }
      });
      lineOptions.append(moveUp, moveDown, remove);
      card.append(lineOptions);
    } else {
      card.append(create("p", "script-text", line.text || "(Empty script line)"));
    }

    const comments = visibleComments(line);
    if (comments.length) {
      const commentList = create("div", "comment-list");
      comments.forEach(comment => {
        const note = create("div", "comment");
        const noteHead = create("div", "comment-head");
        noteHead.append(create("strong", "", comment.author || "Production note"));
        comment.roles.forEach(role => noteHead.append(create("span", "role-tag", role)));
        note.append(noteHead, create("p", "", comment.text));
        commentList.append(note);
      });
      card.append(commentList);
    }
    card.append(makeCommentForm(line));
    elements.scriptList.append(card);
  });
}

function makeCommentForm(line) {
  const form = create("form", "comment-form");
  const toggle = create("button", "comment-toggle", "＋ Add a note");
  toggle.type = "button";
  const body = create("div", "comment-body");
  body.hidden = true;
  toggle.addEventListener("click", () => {
    body.hidden = !body.hidden;
    toggle.textContent = body.hidden ? "＋ Add a note" : "− Cancel note";
  });
  const textarea = create("textarea", "comment-input");
  textarea.maxLength = 2000;
  textarea.placeholder = "Add a production note for this line…";
  const roleRow = create("div", "comment-role-row");
  roleRow.append(create("span", "hint", "Applies to:"));
  const roleChecks = create("div", "comment-role-checks");
  roleCheckboxes(roleChecks, currentState.roles, new Set(), () => {}, "small-checkbox");
  const submit = create("button", "secondary", "Post note");
  submit.type = "submit";
  body.append(textarea, roleRow, submit);
  roleRow.append(roleChecks);
  form.append(toggle, body);
  form.addEventListener("submit", async event => {
    event.preventDefault();
    const roles = [...roleChecks.querySelectorAll("input:checked")].map(input => input.parentElement.textContent);
    try {
      await requestSelectedFileWriteAccess();
      await request("/api/comments", {
        method: "POST",
        body: JSON.stringify({ lineId: line.id, text: textarea.value, roles }),
      });
      body.hidden = true;
      textarea.value = "";
      await fetchState(false);
      if (!dirty) {
        try {
          await writeSelectedFileHandle();
        } catch (error) {
          showToast(`Note saved on the server, but the selected file could not be updated: ${error.message}`);
          return;
        }
      }
      showToast("Note added");
    } catch (error) {
      showToast(error.message);
    }
  });
  return form;
}

function updateSaveButton() {
  const existing = $("#save-script");
  if (currentUser.admin && dirty && !existing) {
    const button = create("button", "primary save-script", "Save changes");
    button.id = "save-script";
    button.addEventListener("click", saveScript);
    $(".script-toolbar").append(button);
  }
}

async function saveScript() {
  try {
    await requestSelectedFileWriteAccess();
    await request("/api/script", {
      method: "PUT",
      body: JSON.stringify({ script: currentState.script, selectedFile: currentState.selectedFile }),
    });
    dirty = false;
    try {
      await writeSelectedFileHandle();
    } catch (error) {
      showToast(`Script saved on the server, but the selected file could not be updated: ${error.message}`);
      return;
    }
    showToast("Script saved");
    await fetchState(true);
  } catch (error) {
    showToast(error.message);
  }
}

function renderSelectedFile() {
  const file = currentState.selectedFile;
  const name = currentState.selectedName || file;
  $("#selected-file").textContent = name || "No text file selected";
  $("#selected-file").title = name || "";
}

async function loadScriptFiles(directory = browserDirectory) {
  try {
    const result = await request(`/api/script-files?path=${encodeURIComponent(directory)}`);
    browserDirectory = result.path;
    $("#file-browser-path").textContent = result.path ? `/${result.path}` : "/";
    const parentButton = $("#file-browser-parent");
    parentButton.disabled = result.parent === null;
    parentButton.dataset.path = result.parent || "";
    const entries = $("#script-file-entries");
    entries.replaceChildren();
    result.entries.forEach(entry => {
      const button = create("button", `file-entry${entry.directory ? " folder-entry" : ""}`);
      button.type = "button";
      button.dataset.path = entry.path;
      button.dataset.directory = String(entry.directory);
      button.append(create("span", "file-entry-icon", entry.directory ? "▸" : "TXT"));
      button.append(create("span", "", entry.name));
      entries.append(button);
    });
    if (!result.entries.length) {
      entries.append(create("p", "hint", "No folders or .txt files in this folder."));
    }
    $("#script-file-browser").hidden = false;
    return true;
  } catch (error) {
    showToast(error.message);
    return false;
  }
}

async function openScriptFile(path) {
  if (dirty && !confirm("Discard unsaved script changes and open another file?")) return;
  try {
    await request("/api/script-files/open", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    selectedFileHandle = null;
    dirty = false;
    $("#script-file-browser").hidden = true;
    await fetchState(true);
    showToast("Script file opened");
  } catch (error) {
    showToast(error.message);
  }
}

async function openLocalScriptFile() {
  if (dirty && !confirm("Discard unsaved script changes and open another file?")) return;
  const picker = document.querySelector("#script-file-input");
  if (window.showOpenFilePicker) {
    try {
      const [handle] = await window.showOpenFilePicker({
        multiple: false,
        types: [{
          description: "Text scripts",
          accept: { "text/plain": [".txt"] },
        }],
      });
      await importLocalScriptFile(await handle.getFile(), handle);
    } catch (error) {
      if (error.name !== "AbortError") showToast(error.message);
    }
    return;
  }
  picker.click();
}

async function importLocalScriptFile(file, fileHandle = null) {
  if (!file || !file.name.toLowerCase().endsWith(".txt")) {
    showToast("Choose a .txt script file.");
    return;
  }
  selectedFileHandle = null;
  try {
    const contents = await file.text();
    const result = await request("/api/script-files/upload", {
      method: "POST",
      body: JSON.stringify({ name: file.name, contents }),
    });
    dirty = false;
    $("#script-file-browser").hidden = true;
    await fetchState(true);
    if (fileHandle && currentState.selectedFile === result.selectedFile) {
      selectedFileHandle = fileHandle;
    }
    showToast(selectedFileHandle
      ? "Script opened; edits will update the selected file"
      : "Script uploaded; edits update the server copy");
  } catch (error) {
    showToast(error.message);
  }
}

async function writeSelectedFileHandle() {
  if (!selectedFileHandle) return;
  const contents = currentState.script.flatMap(line => [
    line.text,
    ...(line.comments || []).map(comment => `N (${comment.text})${comment.roles.join(",")}`),
  ]).join("\n");
  const writable = await selectedFileHandle.createWritable();
  await writable.write(contents);
  await writable.close();
}

async function requestSelectedFileWriteAccess() {
  if (!selectedFileHandle || !selectedFileHandle.requestPermission) return;
  const permission = await selectedFileHandle.requestPermission({ mode: "readwrite" });
  if (permission !== "granted") {
    throw new Error("Write permission was not granted for the selected file.");
  }
}

async function createScriptFile() {
  if (dirty && !confirm("Discard unsaved script changes and create another file?")) return;
  const name = prompt("Name the new script file:", "script.txt");
  if (name === null) return;
  try {
    await request("/api/script-files/create", {
      method: "POST",
      body: JSON.stringify({ directory: browserDirectory, name }),
    });
    dirty = false;
    selectedFileHandle = null;
    $("#script-file-browser").hidden = true;
    await fetchState(true);
    showToast("Script file created");
  } catch (error) {
    showToast(error.message);
  }
}

function moveLine(lineId, offset) {
  const index = currentState.script.findIndex(line => line.id === lineId);
  const target = index + offset;
  if (index < 0 || target < 0 || target >= currentState.script.length) return;
  [currentState.script[index], currentState.script[target]] =
    [currentState.script[target], currentState.script[index]];
  dirty = true;
  renderScript();
}

function addLine() {
  currentState.script.push({
    id: crypto.randomUUID(),
    text: "",
    roles: [],
    important: false,
    comments: [],
  });
  dirty = true;
  renderScript();
  const editors = elements.scriptList.querySelectorAll(".line-editor");
  const last = editors[editors.length - 1];
  last?.focus();
  last?.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function setPlayhead(offset) {
  if (!currentState.script.length) return;
  const visible = currentState.script.filter(isLineVisible);
  if (!visible.length) return;
  const currentId = currentState.playhead.lineId;
  const index = visible.findIndex(line => line.id === currentId);
  const next = index === -1
    ? (offset > 0 ? visible[0] : visible[visible.length - 1])
    : visible[Math.max(0, Math.min(visible.length - 1, index + offset))];
  followingLive = true;
  updateJumpButton();
  try {
    const result = await request("/api/scroll", {
      method: "POST",
      body: JSON.stringify({ lineId: next.id }),
    });
    currentState.playhead = result.playhead;
    lastPlayheadSequence = result.playhead.sequence;
    goToLine(next.id, true);
  } catch (error) {
    showToast(error.message);
  }
}

function goToLine(lineId, showWarning) {
  if (!lineId) return;
  const line = currentState.script.find(item => item.id === lineId);
  if (!dirty) renderScript();
  const element = elements.scriptList.querySelector(`[data-line-id="${CSS.escape(lineId)}"]`);
  if (element && followingLive) {
    programmaticScroll = true;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => { programmaticScroll = false; }, 500);
  }
  if (showWarning && line?.important && isLineVisible(line)) {
    const lineNumber = currentState.script.findIndex(item => item.id === lineId) + 1;
    $("#warning-title").textContent = `Cue ${lineNumber} · Important line`;
    $("#warning-text").textContent = line.text || "This cue is marked important.";
    $("#warning-modal").hidden = false;
    $("#warning-close").focus();
  }
}

function updateJumpButton() {
  $("#jump-live").hidden = followingLive;
}

const preferences = {
  theme: localStorage.getItem("script-cue-theme") || "dark",
  font: localStorage.getItem("script-cue-font") || "Georgia",
  size: Number(localStorage.getItem("script-cue-size") || 24),
};

function applyPreferences() {
  document.documentElement.dataset.theme = preferences.theme;
  document.documentElement.style.setProperty("--script-font", `"${preferences.font}", serif`);
  document.documentElement.style.setProperty("--script-size", `${preferences.size}px`);
  $("#theme-select").value = preferences.theme;
  $("#font-select").value = preferences.font;
  $("#size-range").value = preferences.size;
  $("#size-value").textContent = `${preferences.size} px`;
}

elements.loginForm.addEventListener("submit", async event => {
  event.preventDefault();
  elements.loginError.textContent = "";
  const formData = new FormData(elements.loginForm);
  try {
    const result = await request("/api/login", {
      method: "POST",
      body: JSON.stringify({
        username: formData.get("username"),
        password: formData.get("password"),
      }),
    });
    elements.loginForm.reset();
    showApp(result.user);
  } catch (error) {
    elements.loginError.textContent = error.message;
  }
});

$("#logout-button").addEventListener("click", async () => {
  await request("/api/logout", { method: "POST", body: "{}" });
  currentUser = null;
  showLogin();
});

$("#add-line").addEventListener("click", addLine);
$("#empty-add-line").addEventListener("click", addLine);
$("#browse-script-files").addEventListener("click", openLocalScriptFile);
$("#browse-server-files").addEventListener("click", () => loadScriptFiles(""));
$("#script-file-input").addEventListener("change", event => {
  const file = event.target.files[0];
  if (file) selectedFileHandle = null;
  importLocalScriptFile(file);
  event.target.value = "";
});
$("#create-script-file").addEventListener("click", () => {
  if (!$("#script-file-browser").hidden) {
    createScriptFile();
  } else {
    loadScriptFiles(browserDirectory).then(loaded => {
      if (loaded) createScriptFile();
    });
  }
});
$("#file-browser-parent").addEventListener("click", event => loadScriptFiles(event.currentTarget.dataset.path));
$("#file-browser-close").addEventListener("click", () => { $("#script-file-browser").hidden = true; });
$("#script-file-entries").addEventListener("click", event => {
  const button = event.target.closest("button[data-path]");
  if (!button) return;
  if (button.dataset.directory === "true") loadScriptFiles(button.dataset.path);
  else openScriptFile(button.dataset.path);
});
$("#jump-live").addEventListener("click", () => {
  followingLive = true;
  updateJumpButton();
  goToLine(currentState.playhead.lineId, false);
});

$("#script-list").addEventListener("wheel", () => {
  if (programmaticScroll) return;
  followingLive = false;
  updateJumpButton();
}, { passive: true });
document.addEventListener("scroll", event => {
  if (programmaticScroll || !elements.app.contains(event.target)) return;
  followingLive = false;
  updateJumpButton();
}, true);

document.addEventListener("keydown", event => {
  if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
  if (event.target.closest("input, textarea, select, button")) return;
  if (elements.app.hidden || $("#warning-modal").hidden === false) return;
  event.preventDefault();
  setPlayhead(event.key === "ArrowDown" ? 1 : -1);
});

$("#warning-close").addEventListener("click", () => { $("#warning-modal").hidden = true; });
$("#warning-modal").addEventListener("click", event => {
  if (event.target.id === "warning-modal") $("#warning-modal").hidden = true;
});

$("#settings-toggle").addEventListener("click", () => { $("#settings-panel").hidden = false; });
$("#settings-close").addEventListener("click", () => { $("#settings-panel").hidden = true; });
$("#theme-select").addEventListener("change", event => {
  preferences.theme = event.target.value;
  localStorage.setItem("script-cue-theme", preferences.theme);
  applyPreferences();
});
$("#font-select").addEventListener("change", event => {
  preferences.font = event.target.value;
  localStorage.setItem("script-cue-font", preferences.font);
  applyPreferences();
});
$("#size-range").addEventListener("input", event => {
  preferences.size = Number(event.target.value);
  localStorage.setItem("script-cue-size", preferences.size);
  applyPreferences();
});

$("#add-role-form").addEventListener("submit", async event => {
  event.preventDefault();
  const input = $("#new-role");
  try {
    await request("/api/roles", { method: "POST", body: JSON.stringify({ role: input.value }) });
    input.value = "";
    $("#admin-message").textContent = "Role added.";
    await fetchState(true);
  } catch (error) {
    $("#admin-message").textContent = error.message;
  }
});

$("#add-user-form").addEventListener("submit", async event => {
  event.preventDefault();
  const roles = [...$("#new-user-roles").querySelectorAll("input:checked")]
    .map(input => input.parentElement.textContent);
  try {
    await request("/api/users", {
      method: "POST",
      body: JSON.stringify({
        username: $("#new-user").value,
        password: $("#new-password").value,
        roles,
      }),
    });
    event.target.reset();
    $("#admin-message").textContent = "Account created.";
  } catch (error) {
    $("#admin-message").textContent = error.message;
  }
});

$("#change-password-toggle").addEventListener("click", () => {
  $("#change-password-form").hidden = !$("#change-password-form").hidden;
});
$("#change-username-toggle").addEventListener("click", () => {
  $("#new-username").value = currentUser.username;
  $("#change-username-form").hidden = !$("#change-username-form").hidden;
});
$("#change-username-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = $("#username-message");
  try {
    const result = await request("/api/account/username", {
      method: "POST",
      body: JSON.stringify({ username: $("#new-username").value }),
    });
    currentUser = result.user;
    $("#user-name").textContent = currentUser.username;
    message.textContent = "Username updated.";
  } catch (error) {
    message.textContent = error.message;
  }
});
$("#change-password-form").addEventListener("submit", async event => {
  event.preventDefault();
  const message = $("#password-message");
  if ($("#password-one").value !== $("#password-two").value) {
    message.textContent = "The new passwords do not match.";
    return;
  }
  try {
    await request("/api/account/password", {
      method: "POST",
      body: JSON.stringify({
        oldPassword: $("#old-password").value,
        newPassword: $("#password-one").value,
      }),
    });
    event.target.reset();
    message.textContent = "Password updated.";
  } catch (error) {
    message.textContent = error.message;
  }
});

async function start() {
  try {
    const result = await request("/api/me");
    if (result.user) {
      currentState.roles = result.roles;
      showApp(result.user);
    } else {
      showLogin();
    }
  } catch (_error) {
    showLogin();
  }
}

start();
