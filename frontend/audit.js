/*
 * audit.js — GrowthMate audit trail viewer. Loads /audit into a table.
 */

const rowsEl = document.getElementById("rows");
const refreshBtn = document.getElementById("refresh");
const filterEl = document.getElementById("session-filter");

async function loadAudit() {
  rowsEl.innerHTML = '<tr><td colspan="8">Loading…</td></tr>';
  let url = "/audit";
  const session = filterEl.value.trim();
  if (session) {
    url += `?session_id=${encodeURIComponent(session)}`;
  }
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    render(data);
  } catch (err) {
    rowsEl.innerHTML = '<tr><td colspan="8">Failed to load audit trail.</td></tr>';
  }
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function render(rows) {
  if (!rows.length) {
    rowsEl.innerHTML = '<tr><td colspan="8">No audit records found.</td></tr>';
    return;
  }
  rowsEl.innerHTML = rows
    .map((r) => {
      const decisionClass = r.decision === "BLOCK" ? "badge-block" : "badge-allow";
      return `<tr>
        <td>${r.id}</td>
        <td>${escapeHtml(r.created_at)}</td>
        <td>${escapeHtml(r.session_id)}</td>
        <td>${escapeHtml(r.actor)}</td>
        <td>${escapeHtml(r.event_type)}${r.tool_name ? " · " + escapeHtml(r.tool_name) : ""}</td>
        <td class="${decisionClass}">${escapeHtml(r.decision)}</td>
        <td>${escapeHtml(r.reason || "")}</td>
        <td>${escapeHtml(r.outcome)}</td>
      </tr>`;
    })
    .join("");
}

refreshBtn.addEventListener("click", loadAudit);
filterEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadAudit();
});

loadAudit();
