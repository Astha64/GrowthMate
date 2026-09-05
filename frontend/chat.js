/*
 * chat.js — GrowthMate chat UI. Hardcodes actor:"human" for every /chat
 * request (no actor selector; the buyer-agent journey runs via buyer_agent.py,
 * not the UI).
 *
 * Revision 2: carries the conversation history across turns (via the optional
 * `history` field on /chat) so the multi-turn pipeline works in the UI:
 * clarifying question -> discovery -> selection -> upsell -> checkout ->
 * approval -> payment. Structured output (numbered recommendation lists,
 * checkout previews) is lightly formatted; all text is escaped.
 */

const SESSION_KEY = "growthmate_session";
const messagesEl = document.getElementById("messages");
const formEl = document.getElementById("chat-form");
const inputEl = document.getElementById("input");

let history = [];

function getSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = "sess-human-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// Light formatting for common agent output: bullet/numbered lines and
// "Label: value" checkout lines. All input is escaped before being placed.
function formatReply(text) {
  if (!text) return "";
  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
  const hasList = lines.some((l) => /^\s*(\d+[.)]|[-*])\s/.test(l));
  if (!hasList) {
    return escapeHtml(text).replace(/\n+/g, "<br>");
  }
  let inList = false;
  let html = "";
  for (const line of lines) {
    if (/^\s*(\d+[.)]|[-*])\s/.test(line)) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      const item = line.replace(/^\s*(\d+[.)]|[-*])\s/, "").trim();
      html += "<li>" + escapeHtml(item) + "</li>";
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      html += "<p>" + escapeHtml(line) + "</p>";
    }
  }
  if (inList) html += "</ul>";
  return html;
}

function appendMessage(role, text, isHtml) {
  const div = document.createElement("div");
  div.className = "msg " + (role === "user" ? "user" : "bot");
  if (isHtml) {
    div.innerHTML = text;
  } else {
    div.textContent = text;
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendBlockNotice() {
  const div = document.createElement("div");
  div.className = "msg bot block";
  div.textContent = "(This action was blocked by the safety guardrail and logged to the audit trail.)";
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

async function send(message) {
  appendMessage("user", message);
  const typing = appendMessage("bot", "…");

  const body = {
    session_id: getSessionId(),
    actor: "human",
    message: message,
    history: history,
  };

  try {
    const resp = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (typing) typing.remove();

    const reply = data.reply || "(no reply)";
    appendMessage("bot", formatReply(reply), true);

    // Record the turn in local history so the next message has full context.
    history = history.concat([
      { role: "user", content: message },
      { role: "assistant", content: reply },
    ]);

    if (data.blocked) appendBlockNotice();
  } catch (err) {
    if (typing) typing.remove();
    appendMessage("bot", "Sorry, something went wrong connecting to the server.");
  }
}

formEl.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = "";
  send(text);
});
