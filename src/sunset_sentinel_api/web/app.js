"use strict";

const MAX_VISIBLE_RECORDS = 100;
const MAX_VISIBLE_CHANGES = 20;
const CRITICAL_STATES = new Set(["conflicted", "sunset_overdue"]);
const WARNING_STATES = new Set([
  "deprecated",
  "deprecated_date_unknown",
  "deprecation_scheduled",
  "sunset_scheduled",
]);

const dashboard = {
  records: [],
  changes: [],
  generatedAt: null,
  loading: false,
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  void refreshDashboard();
});

function cacheElements() {
  const ids = [
    "system-status",
    "system-status-text",
    "refresh-button",
    "import-button",
    "metric-total",
    "metric-priority",
    "metric-soon",
    "metric-consumers",
    "record-count",
    "record-filters",
    "record-search",
    "state-filter",
    "sort-records",
    "records-state",
    "records-body",
    "change-count",
    "changes-state",
    "change-list",
    "toast",
  ];
  for (const id of ids) {
    const element = document.getElementById(id);
    if (!element) {
      throw new Error(`Dashboard element is missing: ${id}`);
    }
    elements[id] = element;
  }
}

function bindEvents() {
  elements["refresh-button"].addEventListener("click", () => {
    void refreshDashboard();
  });
  elements["import-button"].addEventListener("click", () => {
    void importSample();
  });
  elements["record-filters"].addEventListener("submit", (event) => {
    event.preventDefault();
  });
  elements["record-search"].addEventListener("input", renderRecords);
  elements["state-filter"].addEventListener("change", renderRecords);
  elements["sort-records"].addEventListener("change", renderRecords);
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    cache: "no-store",
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    throw new Error(`Local API returned status ${response.status}`);
  }
  return response.json();
}

async function refreshDashboard() {
  if (dashboard.loading) {
    return;
  }
  setLoading(true);
  hideState(elements["records-state"]);
  hideState(elements["changes-state"]);
  try {
    const [health, recordsPayload, changesPayload] = await Promise.all([
      requestJson("/api/health"),
      requestJson("/api/records"),
      requestJson("/api/changes"),
    ]);
    dashboard.records = Array.isArray(recordsPayload.records)
      ? recordsPayload.records
      : [];
    dashboard.changes = Array.isArray(changesPayload.changes)
      ? changesPayload.changes
      : [];
    dashboard.generatedAt =
      typeof recordsPayload.generated_at === "string"
        ? recordsPayload.generated_at
        : null;
    setSystemStatus(health.status === "ok" ? "ready" : "error");
    renderMetrics();
    renderRecords();
    renderChanges();
  } catch (_error) {
    setSystemStatus("error");
    showState(
      elements["records-state"],
      "Could not load local lifecycle data. Check the API and try again.",
      true,
    );
    showState(
      elements["changes-state"],
      "Recent changes are unavailable.",
      true,
    );
    showToast("Dashboard refresh failed.", true);
  } finally {
    setLoading(false);
  }
}

async function importSample() {
  if (dashboard.loading) {
    return;
  }
  setLoading(true, "Importing");
  try {
    const summary = await requestJson("/api/import/sample", {
      method: "POST",
      headers: { "X-Sunset-Sentinel": "dashboard-v1" },
    });
    const signalCount = Number(summary.signals || 0);
    const changeCount = Number(summary.changes || 0);
    showToast(
      `Imported ${signalCount} signals. ${changeCount} lifecycle changes recorded.`,
      false,
    );
  } catch (_error) {
    showToast("Sample import failed. The local sample files may be unavailable.", true);
  } finally {
    setLoading(false);
  }
  await refreshDashboard();
}

function setLoading(isLoading, activeLabel = "Refreshing") {
  dashboard.loading = isLoading;
  elements["records-body"].setAttribute("aria-busy", String(isLoading));
  elements["change-list"].setAttribute("aria-busy", String(isLoading));
  elements["refresh-button"].disabled = isLoading;
  elements["import-button"].disabled = isLoading;
  elements["refresh-button"].textContent = isLoading ? activeLabel : "Refresh";
}

function setSystemStatus(status) {
  elements["system-status"].dataset.status = status;
  elements["system-status-text"].textContent =
    status === "ready" ? "Local API ready" : "Local API unavailable";
}

function renderMetrics() {
  const records = dashboard.records;
  const generatedAt = parseDate(dashboard.generatedAt);
  const cutoff = generatedAt
    ? new Date(generatedAt.getTime() + 90 * 24 * 60 * 60 * 1000)
    : null;
  const consumers = new Set();
  let highPriority = 0;
  let withinNinetyDays = 0;

  for (const record of records) {
    if (Number(record.scores?.priority || 0) >= 75) {
      highPriority += 1;
    }
    const sunset = parseDate(record.sunset_at);
    if (sunset && cutoff && sunset >= generatedAt && sunset <= cutoff) {
      withinNinetyDays += 1;
    }
    for (const consumer of arrayValue(record.consumers)) {
      if (typeof consumer.id === "string") {
        consumers.add(consumer.id);
      }
    }
  }

  elements["metric-total"].textContent = String(records.length);
  elements["metric-priority"].textContent = String(highPriority);
  elements["metric-soon"].textContent = String(withinNinetyDays);
  elements["metric-consumers"].textContent = String(consumers.size);
}

function renderRecords() {
  const search = elements["record-search"].value.trim().toLocaleLowerCase();
  const selectedState = elements["state-filter"].value;
  const sort = elements["sort-records"].value;
  const records = dashboard.records
    .filter((record) => recordMatches(record, search, selectedState))
    .sort(recordSorter(sort));
  const visible = records.slice(0, MAX_VISIBLE_RECORDS);

  elements["records-body"].replaceChildren();
  elements["record-count"].textContent =
    records.length === dashboard.records.length
      ? `${records.length} records`
      : `${records.length} of ${dashboard.records.length} records`;

  if (visible.length === 0) {
    elements["records-body"].append(
      tableMessage(
        dashboard.records.length === 0
          ? "No lifecycle records yet. Import the bundled sample to begin."
          : "No records match these filters.",
      ),
    );
    return;
  }

  for (const record of visible) {
    elements["records-body"].append(recordRow(record));
  }
  if (records.length > visible.length) {
    showState(
      elements["records-state"],
      `Showing the first ${MAX_VISIBLE_RECORDS} matching records. Refine the filters to narrow the view.`,
      false,
    );
  } else {
    hideState(elements["records-state"]);
  }
}

function recordMatches(record, search, selectedState) {
  if (selectedState && record.state !== selectedState) {
    return false;
  }
  if (!search) {
    return true;
  }
  const endpoints = arrayValue(record.endpoints)
    .map((endpoint) => `${endpoint.method || ""} ${endpoint.path || ""}`)
    .join(" ");
  const consumers = arrayValue(record.consumers)
    .map((consumer) => `${consumer.id || ""} ${consumer.name || ""}`)
    .join(" ");
  return `${record.target_id || ""} ${endpoints} ${consumers}`
    .toLocaleLowerCase()
    .includes(search);
}

function recordSorter(sort) {
  if (sort === "target") {
    return (left, right) =>
      String(left.target_id || "").localeCompare(String(right.target_id || ""));
  }
  if (sort === "sunset") {
    return (left, right) =>
      dateSortValue(left.sunset_at) - dateSortValue(right.sunset_at);
  }
  return (left, right) =>
    Number(right.scores?.priority || 0) - Number(left.scores?.priority || 0);
}

function recordRow(record) {
  const row = document.createElement("tr");
  const endpoint = arrayValue(record.endpoints)[0] || null;

  const targetCell = cell("Target", "target-cell");
  targetCell.append(element("strong", "", String(record.target_id || "Unknown")));
  targetCell.append(
    element(
      "code",
      "",
      endpoint
        ? `${endpoint.method || ""} ${endpoint.path || ""}`.trim()
        : "Service scope",
    ),
  );
  row.append(targetCell);

  const stateCell = cell("State");
  stateCell.append(stateBadge(String(record.state || "unknown")));
  row.append(stateCell);

  row.append(dateCell("Deprecation", record.deprecation_at));
  row.append(dateCell("Sunset", record.sunset_at));

  const consumersCell = cell("Consumers");
  const consumers = arrayValue(record.consumers);
  const consumerList = element("div", "consumer-list");
  if (consumers.length === 0) {
    consumerList.append(element("span", "date-value is-unknown", "None linked"));
  } else {
    for (const consumer of consumers) {
      consumerList.append(
        element(
          "span",
          "consumer-chip",
          String(consumer.name || consumer.id || "Unnamed"),
        ),
      );
    }
  }
  consumersCell.append(consumerList);
  row.append(consumersCell);

  const priorityCell = cell("Priority");
  const priority = Number(record.scores?.priority || 0);
  priorityCell.append(element("span", "priority-value", `${priority}/100`));
  row.append(priorityCell);

  const contextCell = cell("Context");
  contextCell.append(contextDetails(record));
  row.append(contextCell);
  return row;
}

function contextDetails(record) {
  const details = element("details", "context-details");
  details.append(element("summary", "", "View"));
  const content = element("div", "context-content");
  const scores = record.scores || {};
  content.append(
    paragraphWithLabel(
      "Scores",
      `Urgency ${Number(scores.urgency || 0)}, blast radius ${Number(
        scores.blast_radius || 0,
      )}`,
    ),
  );
  const replacements = arrayValue(record.replacements);
  content.append(
    paragraphWithLabel(
      "Replacement",
      replacements.length ? replacements.join(", ") : "Not specified",
    ),
  );
  const sources = arrayValue(record.signals)
    .map((signal) => String(signal.source || "unknown"))
    .filter((value, index, values) => values.indexOf(value) === index);
  content.append(
    paragraphWithLabel("Sources", sources.length ? sources.join(", ") : "None"),
  );

  const documentation = arrayValue(record.documentation_urls);
  if (documentation.length) {
    const label = element("strong", "", "Documentation");
    content.append(label);
    for (const value of documentation) {
      const url = safeHttpUrl(value);
      if (!url) {
        continue;
      }
      const link = element("a", "", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noreferrer";
      content.append(link);
    }
  }
  details.append(content);
  return details;
}

function renderChanges() {
  const changes = dashboard.changes.slice(0, MAX_VISIBLE_CHANGES);
  elements["change-list"].replaceChildren();
  elements["change-count"].textContent = `${dashboard.changes.length} changes`;

  if (changes.length === 0) {
    showState(
      elements["changes-state"],
      "No material lifecycle changes have been recorded.",
      false,
    );
    return;
  }

  hideState(elements["changes-state"]);
  for (const change of changes) {
    const item = element("li", "change-item");
    const meta = element("div", "change-meta");
    meta.append(
      element("span", "change-type", humanize(String(change.type || "changed"))),
    );
    const time = element("time", "change-time", formatDate(change.recorded_at));
    if (typeof change.recorded_at === "string") {
      time.dateTime = change.recorded_at;
    }
    meta.append(time);
    item.append(meta);
    item.append(
      element("h3", "", String(change.target_id || "Unknown target")),
    );
    const endpoint = change.endpoint;
    item.append(
      element(
        "p",
        "",
        endpoint
          ? `${endpoint.method || ""} ${endpoint.path || ""}`.trim()
          : "Service scope",
      ),
    );
    elements["change-list"].append(item);
  }
}

function cell(label, className = "") {
  const value = element("td", className);
  value.dataset.label = label;
  return value;
}

function dateCell(label, value) {
  const result = cell(label);
  const text = formatDate(value);
  const date = element(
    "span",
    value ? "date-value" : "date-value is-unknown",
    text,
  );
  if (typeof value === "string") {
    date.title = value;
  }
  result.append(date);
  return result;
}

function stateBadge(state) {
  let tone = "is-neutral";
  if (CRITICAL_STATES.has(state)) {
    tone = "is-critical";
  } else if (WARNING_STATES.has(state)) {
    tone = "is-warning";
  }
  return element("span", `state-badge ${tone}`, humanize(state));
}

function paragraphWithLabel(label, value) {
  const paragraph = document.createElement("p");
  paragraph.append(element("strong", "", `${label}: `));
  paragraph.append(document.createTextNode(value));
  return paragraph;
}

function tableMessage(message) {
  const row = element("tr", "empty-row");
  const value = cell("", "");
  value.colSpan = 7;
  value.textContent = message;
  row.append(value);
  return row;
}

function element(tagName, className = "", text = "") {
  const value = document.createElement(tagName);
  if (className) {
    value.className = className;
  }
  if (text) {
    value.textContent = text;
  }
  return value;
}

function showState(target, message, isError) {
  target.textContent = message;
  target.classList.add("is-visible");
  target.classList.toggle("is-error", isError);
}

function hideState(target) {
  target.textContent = "";
  target.classList.remove("is-visible", "is-error");
}

let toastTimer = null;

function showToast(message, isError) {
  if (toastTimer) {
    window.clearTimeout(toastTimer);
  }
  elements["toast"].textContent = message;
  elements["toast"].classList.toggle("is-error", isError);
  elements["toast"].hidden = false;
  toastTimer = window.setTimeout(() => {
    elements["toast"].hidden = true;
  }, 5000);
}

function formatDate(value) {
  const parsed = parseDate(value);
  if (!parsed) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: "UTC",
  }).format(parsed);
}

function parseDate(value) {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function dateSortValue(value) {
  const parsed = parseDate(value);
  return parsed ? parsed.getTime() : Number.POSITIVE_INFINITY;
}

function humanize(value) {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function safeHttpUrl(value) {
  if (typeof value !== "string") {
    return null;
  }
  try {
    const parsed = new URL(value);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return parsed.toString();
  } catch (_error) {
    return null;
  }
}

function arrayValue(value) {
  return Array.isArray(value) ? value : [];
}
