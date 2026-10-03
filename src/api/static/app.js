/* Engineering Atlas: no external scripts; source and model text never become HTML. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  view: "ask", sessionId: null, busy: false, controller: null,
  documents: [], sessions: [], activity: [], health: null,
  token: readToken(), pendingConfirmation: null, lastQuestion: "",
};
const stages = [
  ["A", "Request entrance"], ["B", "Coordinator"], ["C", "Source readers"],
  ["D", "Evidence checker"], ["E", "Answer writer"], ["F", "Response checker"],
];

function readToken() {
  try { return sessionStorage.getItem("atlas-access-token") || "local-demo-token"; }
  catch { return "local-demo-token"; }
}
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function icon(name, className = "icon") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function clear(element) { element.replaceChildren(); }
function cleanLabel(value, fallback = "") { return typeof value === "string" ? value : fallback; }
function sourceName(value) {
  const source = String(value || "document").toLowerCase();
  if (source.includes("jira")) return "Jira";
  if (source.includes("slack")) return "Slack";
  return "Document";
}
function sourceIcon(value) { return { Jira: "jira", Slack: "slack", Document: "file" }[sourceName(value)]; }
function listFrom(data, key) { return Array.isArray(data) ? data : Array.isArray(data?.[key]) ? data[key] : []; }
function formattedDate(value, relative = false) {
  if (!value) return "Just now";
  const date = new Date(typeof value === "number" && value < 100000000000 ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return String(value);
  if (relative) {
    const minutes = Math.floor((Date.now() - date.getTime()) / 60000);
    if (minutes < 1) return "Just now";
    if (minutes < 60) return `${minutes}m ago`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)}h ago`;
  }
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(date);
}
function statusText(status) {
  const labels = {
    complete: "Checked answer", completed: "Checked answer", success: "Checked answer", ok: "Checked answer",
    answered: "Checked answer", partial: "Partial answer", insufficient_evidence: "More evidence needed",
    blocked: "Request blocked", error: "Needs attention", failed: "Needs attention", refused: "Request declined",
    no_evidence: "No supporting evidence", no_answer: "More evidence needed", limited: "Limited evidence",
    unavailable: "Sources unavailable", cancelled: "Stopped",
  };
  return labels[status] || String(status || "Checked answer").replaceAll("_", " ");
}
function toast(message, error = false) {
  const item = node("div", `toast${error ? " error" : ""}`, message);
  $("toast-container").append(item);
  setTimeout(() => item.remove(), 6500);
}
async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail;
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : data.message || data.error;
      if (!detail && Array.isArray(data.detail)) detail = data.detail.map((item) => item.msg).join("; ");
    } catch { /* An upstream error may not be JSON. */ }
    const messages = {
      administrator_required: "An administrator account is required to manage documents.",
      invalid_request: "This request could not be accepted. Check your input and try again.",
      question_failed_content_check: "This question could not pass the content check. Rephrase it without credentials or instructions to bypass access rules.",
      session_unavailable: "This conversation has expired or its sources changed. Start a new conversation to retrieve fresh evidence.",
      document_unavailable: "This document is no longer available.",
      unsupported_document_format: "Choose a Markdown, text, PDF, or Word (.docx) document.",
      document_too_large: "This document exceeds the workspace's upload limit. Choose a smaller file.",
      request_too_large: "This request is too large. Try a smaller file or shorter question.",
      document_quarantined: "This document was not indexed because it did not pass the content checks.",
      document_could_not_be_processed: "This document could not be processed. Check that it contains readable text and is not encrypted.",
    };
    detail = messages[detail] || detail;
    if (response.status === 401) throw new Error("Access token not accepted. Update your connection settings to reconnect.");
    if (response.status === 403) throw new Error(detail || "Your account does not have permission for this action.");
    if (response.status === 404) throw new Error(detail || "This item is no longer available. It may have expired or been removed.");
    throw new Error(detail || `The workspace could not complete this request (${response.status}). Please try again.`);
  }
  return response;
}
async function getJSON(path) { return (await api(path)).json(); }
function emptyState(container, title, description, iconName = "file", retry) {
  clear(container);
  const wrap = node("div", "empty-state");
  wrap.append(icon(iconName), node("h3", "", title), node("p", "", description));
  if (retry) {
    const button = node("button", "retry-button", "Try again");
    button.addEventListener("click", retry);
    wrap.append(button);
  }
  container.append(wrap);
}
function loadingState(container) {
  clear(container);
  for (let i = 0; i < 3; i++) {
    const row = node("div", "loading-row");
    row.append(node("div", "skeleton-line"), node("div", "skeleton-line"));
    container.append(row);
  }
}
function setView(view) {
  state.view = view;
  ["ask", "knowledge", "activity"].forEach((name) => { $(`view-${name}`).hidden = name !== view; });
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  $("view-title").textContent = { ask: "Ask assistant", knowledge: "Knowledge", activity: "Activity" }[view];
  closeSidebar();
  if (view === "knowledge") void loadDocuments();
  if (view === "activity") void loadActivity();
}
function closeSidebar() {
  $("sidebar").classList.remove("open");
  $("sidebar-backdrop").hidden = true;
  $("mobile-menu").setAttribute("aria-expanded", "false");
}
function newConversation() {
  if (state.busy) state.controller?.abort();
  state.sessionId = null;
  clear($("conversation"));
  $("conversation").hidden = true;
  $("welcome").hidden = false;
  document.querySelector(".chat-column").classList.remove("has-conversation");
  $("question").value = "";
  updateComposer();
  setView("ask");
  renderSessions();
  $("question").focus();
}
function updateComposer() {
  const question = $("question");
  $("character-count").textContent = `${question.value.length.toLocaleString()} / 4,000`;
  $("send-button").disabled = state.busy || !question.value.trim();
  question.style.height = "auto";
  question.style.height = `${Math.min(question.scrollHeight, 180)}px`;
}
function showConversation() {
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  document.querySelector(".chat-column").classList.add("has-conversation");
}
function scrollToMessage(message) {
  if (state.view !== "ask") return;
  requestAnimationFrame(() => message.scrollIntoView({ behavior: "smooth", block: "nearest" }));
}
function messageShell(role) {
  const message = node("article", `message ${role}`);
  const header = node("div", "message-header");
  const avatar = node("span", "message-avatar", role === "user" ? "DE" : undefined);
  if (role !== "user") avatar.append(icon("spark"));
  header.append(avatar, node("span", "", role === "user" ? "You" : "Atlas"));
  message.append(header);
  $("conversation").append(message);
  return message;
}
function addUserMessage(question) {
  const message = messageShell("user");
  message.append(node("div", "message-text", question));
  return message;
}
function progressPanel(message) {
  const panel = node("div", "progress-panel");
  const title = node("div", "progress-title");
  title.append(node("span", "spinner"), node("span", "", "Finding the context behind your question…"));
  panel.append(title);
  stages.forEach(([letter, label]) => {
    const step = node("div", "progress-step");
    step.dataset.stage = letter;
    step.append(node("span", "step-mark", letter), node("span", "", label));
    panel.append(step);
  });
  message.append(panel);
  return panel;
}
function updateProgress(panel, data) {
  const raw = String(data.stage || data.node || data.step || "");
  const normalized = raw.toLowerCase();
  const aliases = [
    ["request", "entrance", "input", "authenticate", "validate_input"],
    ["coordinator", "plan", "route"], ["source", "reader", "retrieve", "jira", "slack", "document"],
    ["evidence", "aggregate", "assess"], ["writer", "generate", "draft", "synthesize"],
    ["response", "release", "output", "finalize"],
  ];
  let index = stages.findIndex(([letter]) => raw === letter || raw.startsWith(`${letter}.`) || raw.startsWith(`${letter}:`));
  if (index < 0) index = aliases.findIndex((words) => words.some((word) => normalized.includes(word)));
  if (index < 0) index = 0;
  panel.querySelectorAll(".progress-step").forEach((step, stepIndex) => {
    step.classList.toggle("completed", stepIndex < index);
    step.classList.toggle("current", stepIndex === index);
    step.querySelector(".step-mark").textContent = stepIndex < index ? "✓" : stages[stepIndex][0];
    if (stepIndex === index) step.lastChild.textContent = data.message || stages[stepIndex][1];
  });
}
function completeProgress(panel) {
  panel.classList.add("completed");
  const title = panel.querySelector(".progress-title");
  clear(title);
  title.append(icon("check"), node("span", "", "Source and response checks complete"));
  title.querySelector(".icon").style.width = "12px";
  title.querySelector(".icon").style.height = "12px";
}
function appendAnswerText(container, answer, citations) {
  // Render only text, emphasis, and known citation buttons. Never interpret source HTML.
  const pattern = /(\*\*[^*\n]+\*\*|\[(?:\d+|S\d+)\])/g;
  let lastIndex = 0;
  for (const match of String(answer).matchAll(pattern)) {
    container.append(document.createTextNode(answer.slice(lastIndex, match.index)));
    if (match[0].startsWith("**")) {
      container.append(node("strong", "", match[0].slice(2, -2)));
    } else {
      const id = match[0].slice(1, -1);
      const citation = citations.find((item, index) => String(item.id ?? index + 1) === id);
      if (citation) {
        const button = node("button", "citation-inline", id);
        button.setAttribute("aria-label", `Open source ${id}: ${citation.title || "Source"}`);
        button.addEventListener("click", () => void showCitation(citation));
        container.append(button);
      } else container.append(document.createTextNode(match[0]));
    }
    lastIndex = match.index + match[0].length;
  }
  container.append(document.createTextNode(answer.slice(lastIndex)));
}
function renderAnswer(message, data, panel) {
  if (panel) completeProgress(panel);
  const answer = cleanLabel(data.answer, cleanLabel(data.content, "No answer was returned."));
  const citations = Array.isArray(data.citations) ? data.citations : [];
  const status = cleanLabel(data.status, "complete");
  if (panel && data.source_status) {
    Object.entries(data.source_status).forEach(([name, result]) => {
      const indicator = $(`source-${name}`);
      if (!indicator) return;
      const available = ["success", "partial", "empty"].includes(result);
      indicator.className = `source-state ${available ? "connected" : "disconnected"}`;
      indicator.title = `${name}: ${String(result).replaceAll("_", " ")} on the latest request`;
    });
  }
  const badge = node("span", `response-status ${status.replace(/[^a-z_]/gi, "")}`, statusText(status));
  message.querySelector(".message-header").append(badge);
  const body = node("div", "answer-body");
  appendAnswerText(body, answer, citations);
  message.append(body);
  if (citations.length) {
    const list = node("div", "citation-list");
    list.setAttribute("aria-label", "Answer sources");
    citations.forEach((citation, index) => {
      const button = node("button", "citation-card");
      button.append(node("span", "citation-number", citation.id ?? index + 1), icon(sourceIcon(citation.source)), node("span", "citation-card-title", citation.title || "Source"), node("span", "source-type", sourceName(citation.source)));
      button.addEventListener("click", () => void showCitation(citation));
      list.append(button);
    });
    message.append(list);
  }
  const limitations = Array.isArray(data.limitations) ? data.limitations : data.limitations ? [data.limitations] : [];
  if (limitations.length) {
    const limitation = node("div", "limitation");
    limitation.append(icon("info"), node("span", "", limitations.map((item) => typeof item === "string" ? item : item.message || item.description || JSON.stringify(item)).join("\n")));
    message.append(limitation);
  }
  const meta = node("div", "response-meta");
  const requestId = node("span", "request-id", data.request_id ? `Request ${data.request_id}` : "Based on retrieved source evidence");
  const copy = node("button", "icon-button");
  copy.setAttribute("aria-label", "Copy answer");
  copy.append(icon("copy"));
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(answer); toast("Answer copied to clipboard."); }
    catch { toast("Clipboard access is unavailable. Select the answer text to copy it.", true); }
  });
  meta.append(requestId, copy);
  message.append(meta);
}
async function submitQuestion(question) {
  if (state.busy || !question.trim()) return;
  if (question.length > 4000) { toast("Keep your question under 4,000 characters.", true); return; }
  state.busy = true;
  state.lastQuestion = question;
  const controller = new AbortController();
  state.controller = controller;
  $("stop-button").hidden = false;
  $("question").value = "";
  updateComposer();
  showConversation();
  addUserMessage(question);
  const message = messageShell("assistant");
  const progress = progressPanel(message);
  scrollToMessage(message);
  let answered = false;
  try {
    const response = await api("/api/chat", {
      method: "POST", body: JSON.stringify({ question, ...(state.sessionId ? { session_id: state.sessionId } : {}) }),
      signal: controller.signal,
    });
    const onAnswer = (data) => {
      if (answered) return;
      answered = true;
      state.sessionId = data.session_id || state.sessionId;
      renderAnswer(message, data, progress);
      scrollToMessage(message);
    };
    if (response.headers.get("Content-Type")?.includes("application/json")) {
      onAnswer(await response.json());
    } else {
      await readEventStream(response, (event, data) => {
        if (event === "progress" || event === "status") updateProgress(progress, data);
        else if (event === "answer" || data.type === "answer") onAnswer(data);
        else if (event === "error") throw new Error(data.message || data.detail || "The answer could not be completed. Please try again.");
        else if (data.session_id) state.sessionId = data.session_id;
      });
    }
    if (!answered) throw new Error("The connection ended before a checked answer arrived. Please try again.");
  } catch (error) {
    if (!answered) {
      progress.remove();
      const cancelled = error.name === "AbortError";
      const errorBox = node("div", "message-error", cancelled ? "Stopped. No unchecked answer was released." : error.message || "Cannot reach the workspace. Check that the server is running.");
      if (!cancelled) {
        errorBox.append(node("br"));
        const retry = node("button", "retry-button", "Try this question again");
        retry.addEventListener("click", () => void submitQuestion(question));
        errorBox.append(retry);
      }
      message.append(errorBox);
    }
  } finally {
    if (state.controller === controller) {
      state.busy = false;
      state.controller = null;
      $("stop-button").hidden = true;
      updateComposer();
    }
    void loadSessions();
  }
}
async function readEventStream(response, callback) {
  if (!response.body) throw new Error("Streaming is unavailable in this browser.");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  function dispatch(block) {
    let event = "message";
    const dataLines = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    const raw = dataLines.join("\n");
    if (raw === "[DONE]") return;
    let data;
    try { data = JSON.parse(raw); } catch { throw new Error("The server returned an unreadable response. Please try again."); }
    callback(event, data);
  }
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replaceAll("\r\n", "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) { if (buffer.trim()) dispatch(buffer); break; }
    }
  } finally { reader.releaseLock(); }
}

async function loadHealth() {
  try {
    const health = await getJSON("/api/health");
    state.health = health;
    const demo = health.mode === "demo";
    const synthetic = health.source_mode === "synthetic" || demo;
    $("mode-badge").replaceChildren(node("span"), document.createTextNode(demo ? "Demo environment" : synthetic ? "Synthetic sources" : "Connected workspace"));
    $("demo-note").hidden = !synthetic;
    $("demo-note").textContent = demo ? "You're exploring a local demo with synthetic engineering data." : "Connected model · synthetic engineering sources for development.";
    document.querySelectorAll(".source-label:not(.neutral)").forEach((label) => { label.textContent = synthetic ? "Synthetic data" : "Connected source"; });
    const healthy = !["error", "unhealthy", "down", "unavailable", "degraded"].includes(String(health.status).toLowerCase());
    $("sidebar-status-dot").className = `status-dot ${healthy ? "good" : "bad"}`;
    $("sidebar-status").textContent = healthy ? "Workspace is connected" : "Workspace needs attention";
    $("sidebar-status-sub").textContent = synthetic ? "Local workspace · synthetic sources" : "Source availability checked";
    if (health.session_ttl_seconds) $("session-list").title = `Recent conversations expire after ${Math.round(health.session_ttl_seconds / 60)} minutes of inactivity.`;
    const services = health.services || health.sources || {};
    ["jira", "slack", "documents"].forEach((name) => {
      const service = services[name] ?? services[`${name}_mcp`] ?? (name === "documents" ? services.document_index : undefined);
      let connected = false;
      if (typeof service === "boolean") connected = service;
      else if (typeof service === "string") connected = ["ok", "healthy", "connected", "ready", "available", "up"].includes(service.toLowerCase());
      else if (service) connected = service.healthy === true || service.available === true || ["ok", "healthy", "connected", "ready", "available", "up"].includes(String(service.status).toLowerCase());
      const configured = typeof service === "string" && service.toLowerCase().includes("synthetic");
      const indicator = $(`source-${name}`);
      indicator.className = `source-state${service === undefined || configured ? "" : connected ? " connected" : " disconnected"}`;
      indicator.title = configured ? "Synthetic MCP source configured; checked when queried" : service === undefined ? "Source status not reported" : connected ? "Source available" : "Source unavailable or degraded";
    });
    if (!demo) {
      document.querySelector(".profile strong").textContent = "Engineering workspace";
      document.querySelector(".profile small").textContent = "Connected account";
    }
  } catch {
    $("sidebar-status-dot").className = "status-dot bad";
    $("sidebar-status").textContent = "Workspace is offline";
    $("sidebar-status-sub").textContent = "Check your server and access token";
    ["jira", "slack", "documents"].forEach((name) => { $(`source-${name}`).className = "source-state disconnected"; });
  }
}
async function loadSessions() {
  try {
    const data = await getJSON("/api/sessions");
    state.sessions = listFrom(data, "sessions");
    renderSessions();
  } catch (error) {
    clear($("session-list"));
    const message = node("p", "sidebar-empty", "Conversations are unavailable.");
    message.title = error.message;
    $("session-list").append(message);
  }
}
function renderSessions() {
  const container = $("session-list");
  clear(container);
  if (!state.sessions.length) {
    container.append(node("p", "sidebar-empty", "Your recent conversations will appear here. Sessions expire automatically."));
    return;
  }
  for (const session of state.sessions) {
    const id = session.id || session.session_id;
    const title = session.title || session.question || session.last_question || "Engineering question";
    const row = node("div", `session-row${state.sessionId === id ? " selected" : ""}`);
    const button = node("button", "session-button", title);
    button.title = `${title}\n${formattedDate(session.updated_at || session.created_at, true)} · Temporary session`;
    button.addEventListener("click", () => void openSession(id));
    const remove = node("button", "icon-button");
    remove.setAttribute("aria-label", `Delete conversation: ${title}`);
    remove.append(icon("trash"));
    remove.addEventListener("click", () => void deleteSession(id));
    row.append(button, remove);
    container.append(row);
  }
}
async function openSession(id) {
  if (state.busy) { toast("Finish or stop the current answer before opening another conversation."); return; }
  try {
    const data = await getJSON(`/api/sessions/${encodeURIComponent(id)}`);
    state.sessionId = id;
    clear($("conversation"));
    showConversation();
    let messages = listFrom(data, "messages");
    if (!messages.length && Array.isArray(data.history)) messages = data.history;
    if (!messages.length && Array.isArray(data.turns)) messages = data.turns;
    for (const message of messages) {
      if (message.role === "user") addUserMessage(message.content || message.question || "");
      else if (message.role === "assistant") renderAnswer(messageShell("assistant"), message);
      else if (message.question) {
        addUserMessage(message.question);
        renderAnswer(messageShell("assistant"), message.response || message);
      }
    }
    if (!messages.length) emptyState($("conversation"), "Start a follow-up", "This session has no retained messages. Ask a question to search the sources again.", "message");
    setView("ask");
    renderSessions();
    $("question").focus();
  } catch (error) { toast(error.message, true); void loadSessions(); }
}
async function deleteSession(id) {
  if (state.busy && state.sessionId === id) { toast("Finish or stop this answer before deleting its conversation."); return; }
  const confirmed = await confirmAction("Delete conversation?", "This removes the temporary conversation history from this workspace. Your source documents stay available.", "Delete");
  if (!confirmed) return;
  try {
    await api(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (state.sessionId === id) newConversation();
    await loadSessions();
    toast("Conversation deleted.");
  } catch (error) { toast(error.message, true); }
}
async function loadDocuments() {
  if (!state.documents.length) loadingState($("document-list"));
  try {
    const data = await getJSON("/api/documents");
    state.documents = listFrom(data, "documents");
    const count = state.documents.length;
    $("document-count").textContent = count;
    $("library-document-count").textContent = `${count} indexed document${count === 1 ? "" : "s"}`;
    $("table-document-count").textContent = count;
    renderDocuments();
  } catch (error) { emptyState($("document-list"), "The library could not be loaded", error.message, "file", loadDocuments); }
}
function renderDocuments() {
  const container = $("document-list");
  const query = $("document-search").value.trim().toLowerCase();
  const documents = state.documents.filter((document) => `${document.title || document.filename || ""} ${document.source || ""}`.toLowerCase().includes(query));
  clear(container);
  if (!documents.length) {
    emptyState(container, query ? "No matching documents" : "Your library is ready to grow", query ? "Try a different title or source name." : "Add a document above to make it available to the assistant.");
    return;
  }
  documents.forEach((document) => {
    const row = node("div", "document-row");
    const mark = node("span", "source-logo docs");
    mark.append(icon("file"));
    const details = node("div", "document-details");
    const title = document.title || document.filename || "Untitled document";
    details.append(node("strong", "", title));
    const meta = node("div", "document-meta");
    const chunkCount = document.chunk_count ?? document.chunks ?? 0;
    meta.append(node("span", "", sourceName(document.source)), node("span", "", `${chunkCount} passage${chunkCount === 1 ? "" : "s"}`), node("span", "", formattedDate(document.updated_at || document.created_at)));
    details.append(meta);
    const remove = node("button", "icon-button");
    remove.setAttribute("aria-label", `Remove ${title}`);
    remove.append(icon("trash"));
    remove.addEventListener("click", () => void removeDocument(document));
    row.append(mark, details, node("span", "document-state", document.status || "Indexed"), remove);
    container.append(row);
  });
}
async function uploadDocument(file) {
  if (!file) return;
  const status = $("upload-status");
  status.hidden = false;
  if (!/\.(md|markdown|txt|pdf|docx)$/i.test(file.name)) {
    status.className = "upload-status error";
    status.textContent = "Choose a Markdown, text, PDF, or Word (.docx) document.";
    return;
  }
  if (file.size > 10000000) {
    status.className = "upload-status error";
    status.textContent = "This document is larger than 10 MB. Choose a smaller file.";
    return;
  }
  const form = new FormData();
  form.append("file", file);
  status.className = "upload-status pending";
  status.textContent = `Screening and indexing ${file.name}…`;
  $("upload-trigger").disabled = true;
  $("browse-upload").disabled = true;
  $("file-input").disabled = true;
  try {
    const response = await api("/api/documents/upload", { method: "POST", body: form });
    const data = await response.json();
    status.className = "upload-status";
    status.textContent = data.message || `${file.name} is indexed and ready for questions.`;
    await loadDocuments();
  } catch (error) {
    status.className = "upload-status error";
    status.textContent = error.message;
  } finally {
    $("upload-trigger").disabled = false;
    $("browse-upload").disabled = false;
    $("file-input").disabled = false;
    $("file-input").value = "";
  }
}
async function removeDocument(document) {
  const title = document.title || document.filename || "this document";
  if (!await confirmAction("Remove document?", `Remove “${title}” and its indexed passages? Future answers will no longer retrieve this document.`, "Remove document")) return;
  try {
    await api(`/api/documents/${encodeURIComponent(document.id || document.document_id)}`, { method: "DELETE" });
    await loadDocuments();
    await loadSessions();
    if (state.sessionId && !state.sessions.some((session) => (session.id || session.session_id) === state.sessionId)) {
      state.sessionId = null;
      clear($("conversation"));
      $("conversation").hidden = true;
      $("welcome").hidden = false;
      document.querySelector(".chat-column").classList.remove("has-conversation");
    }
    toast("Document removed from the search index.");
  } catch (error) { toast(error.message, true); }
}
async function loadActivity() {
  loadingState($("activity-list"));
  try {
    const data = await getJSON("/api/activity");
    state.activity = listFrom(data, "activity");
    if (!state.activity.length && Array.isArray(data.events)) state.activity = data.events;
    if (!state.activity.length && Array.isArray(data.requests)) state.activity = data.requests;
    renderActivity();
  } catch (error) { emptyState($("activity-list"), "Activity is unavailable", error.message, "clock", loadActivity); }
}
function renderActivity() {
  const container = $("activity-list");
  clear(container);
  const activities = state.activity;
  $("stat-total").textContent = activities.length;
  $("stat-success").textContent = activities.filter((item) => ["ok", "success", "complete", "completed", "answered", "partial"].includes(item.status)).length;
  const durations = activities.map((item) => item.duration_ms ?? item.elapsed_ms).filter((value) => typeof value === "number");
  $("stat-duration").textContent = durations.length ? `${(durations.reduce((sum, item) => sum + item, 0) / durations.length / 1000).toFixed(1)}s` : "—";
  if (!activities.length) { emptyState(container, "A quiet workspace, for now", "Ask your first question to see its outcome here.", "clock"); return; }
  activities.forEach((activity) => {
    const row = node("div", "activity-row");
    const mark = node("span", "activity-mark");
    mark.append(icon("spark"));
    const details = node("div", "activity-details");
    details.append(node("strong", "", activity.question || activity.title || "Engineering question"));
    const meta = node("p");
    meta.append(node("span", "", formattedDate(activity.created_at || activity.timestamp, true)));
    const duration = activity.duration_ms ?? activity.elapsed_ms;
    if (typeof duration === "number") meta.append(node("span", "", `${(duration / 1000).toFixed(1)}s`));
    const sources = Array.isArray(activity.sources) ? activity.sources.join(", ") : "";
    if (sources) meta.append(node("span", "", sources));
    if (activity.request_id || activity.id) meta.append(node("span", "request-id", activity.request_id || activity.id));
    details.append(meta);
    const status = String(activity.status || "complete");
    row.append(mark, details, node("span", `response-status ${status.replace(/[^a-z_]/gi, "")}`, statusText(status)));
    container.append(row);
  });
}
function safeSourceURL(raw) {
  if (!raw || typeof raw !== "string") return null;
  try {
    const url = new URL(raw, location.origin);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    if (url.origin === location.origin && !/^\/api\/documents\/.+\/content$/.test(url.pathname)) return null;
    return url;
  } catch { return null; }
}
async function showCitation(citation) {
  const dialog = $("citation-dialog");
  $("citation-title").textContent = citation.title || "Source details";
  const container = $("citation-content");
  clear(container);
  const values = [["Source", sourceName(citation.source)], ["Location", citation.location || citation.locator || "Source record"], ["Updated", formattedDate(citation.updated_at || citation.timestamp)], ["Reference", citation.source_id || citation.document_id || citation.id]];
  values.forEach(([label, value]) => {
    if (value === undefined || value === null) return;
    const line = node("div", "citation-detail-line");
    line.append(node("span", "", label), node("span", "", typeof value === "object" ? JSON.stringify(value) : value));
    container.append(line);
  });
  const excerptText = citation.excerpt || citation.snippet || citation.content;
  if (typeof excerptText === "string") container.append(node("div", "citation-excerpt", excerptText));
  const url = safeSourceURL(citation.url);
  if (url?.origin === location.origin) {
    const preview = node("div", "citation-excerpt", "Loading approved document text…");
    container.append(preview);
    if (!dialog.open) dialog.showModal();
    try {
      const response = await api(`${url.pathname}${url.search}`);
      const contentType = response.headers.get("Content-Type") || "";
      if (contentType.includes("application/json")) {
        const data = await response.json();
        preview.textContent = data.content || data.text || data.document?.content || "No text preview is available for this document.";
      } else preview.textContent = await response.text();
    } catch (error) { preview.textContent = error.message; }
  } else if (url) {
    const link = node("a", "primary-button citation-link", "Open source");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.append(icon("link"));
    container.append(link);
    if (state.health?.source_mode === "synthetic" || state.health?.mode === "demo") container.append(node("p", "citation-unavailable", "This is a synthetic demo source. Its original workspace link may not be accessible."));
  } else {
    container.append(node("p", "citation-unavailable", "No external link is available for this source."));
  }
  if (!dialog.open) dialog.showModal();
}
function openSettings() {
  $("access-token").value = state.token;
  $("settings-dialog").showModal();
}
function confirmAction(title, message, action) {
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("accept-confirm").textContent = action;
  $("confirm-dialog").showModal();
  return new Promise((resolve) => { state.pendingConfirmation = resolve; });
}
function closeConfirmation(accepted) {
  $("confirm-dialog").close();
  state.pendingConfirmation?.(accepted);
  state.pendingConfirmation = null;
}

document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
$("new-chat").addEventListener("click", newConversation);
$("manage-knowledge").addEventListener("click", () => setView("knowledge"));
$("question").addEventListener("input", updateComposer);
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!state.busy) void submitQuestion($("question").value.trim());
  }
});
$("chat-form").addEventListener("submit", (event) => { event.preventDefault(); void submitQuestion($("question").value.trim()); });
$("stop-button").addEventListener("click", () => state.controller?.abort());
document.querySelectorAll("[data-question]").forEach((button) => button.addEventListener("click", () => { $("question").value = button.dataset.question; updateComposer(); $("question").focus(); }));
$("mobile-menu").addEventListener("click", () => { $("sidebar").classList.add("open"); $("sidebar-backdrop").hidden = false; $("mobile-menu").setAttribute("aria-expanded", "true"); });
$("sidebar-backdrop").addEventListener("click", closeSidebar);
$("open-settings").addEventListener("click", openSettings);
$("top-settings").addEventListener("click", openSettings);
$("close-settings").addEventListener("click", () => $("settings-dialog").close());
$("cancel-settings").addEventListener("click", () => $("settings-dialog").close());
$("close-citation").addEventListener("click", () => $("citation-dialog").close());
$("close-confirm").addEventListener("click", () => closeConfirmation(false));
$("cancel-confirm").addEventListener("click", () => closeConfirmation(false));
$("accept-confirm").addEventListener("click", () => closeConfirmation(true));
$("confirm-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closeConfirmation(false); });
$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.busy) { toast("Stop the current answer before changing connections."); return; }
  state.token = $("access-token").value.trim();
  try { sessionStorage.setItem("atlas-access-token", state.token); } catch { /* Storage may be blocked; keep the token in memory. */ }
  $("settings-dialog").close();
  state.sessionId = null;
  newConversation();
  void Promise.allSettled([loadHealth(), loadSessions(), loadDocuments()]);
  toast("Connection settings saved.");
});
$("refresh-sessions").addEventListener("click", () => void loadSessions());
$("refresh-documents").addEventListener("click", () => void loadDocuments());
$("refresh-activity").addEventListener("click", () => void loadActivity());
$("document-search").addEventListener("input", renderDocuments);
$("upload-trigger").addEventListener("click", () => $("file-input").click());
$("browse-upload").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (event) => void uploadDocument(event.target.files[0]));
$("upload-zone").addEventListener("dragover", (event) => { event.preventDefault(); if (!$("file-input").disabled) $("upload-zone").classList.add("dragover"); });
$("upload-zone").addEventListener("dragleave", () => $("upload-zone").classList.remove("dragover"));
$("upload-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  $("upload-zone").classList.remove("dragover");
  if ($("file-input").disabled) return;
  if (event.dataTransfer.files.length > 1) toast("Add one document at a time.");
  void uploadDocument(event.dataTransfer.files[0]);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeSidebar();
  if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.metaKey && !event.altKey && !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName) && !document.querySelector("dialog[open]")) newConversation();
});
window.addEventListener("online", () => void loadHealth());
void Promise.allSettled([loadHealth(), loadSessions(), loadDocuments()]);
updateComposer();
