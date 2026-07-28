const recordCount = document.querySelector("[data-record-count]");
const highestPriority = document.querySelector("[data-highest-priority]");
const recordsBody = document.querySelector("[data-records-body]");
const loadState = document.querySelector("[data-load-state]");

const stateLabels = {
  deprecated: ["Deprecated", "state--high"],
  deprecated_date_unknown: ["Date unknown", "state--medium"],
  deprecation_scheduled: ["Scheduled", "state--scheduled"],
};

function formatDate(value) {
  if (!value) {
    return "Not declared";
  }
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(value));
}

function addCell(row, content) {
  const cell = document.createElement("td");
  if (content instanceof Node) {
    cell.append(content);
  } else {
    cell.textContent = content;
  }
  row.append(cell);
}

function targetCell(record) {
  const wrapper = document.createElement("div");
  const target = document.createElement("strong");
  const scope = document.createElement("code");
  const endpoint = record.endpoints[0];

  target.textContent = record.target_id;
  scope.textContent = endpoint ? `${endpoint.method} ${endpoint.path}` : "Service scope";
  wrapper.append(target, scope);
  return wrapper;
}

function stateCell(record) {
  const badge = document.createElement("span");
  const [label, className] = stateLabels[record.state] || [record.state, ""];
  badge.className = `state ${className}`.trim();
  badge.textContent = label;
  return badge;
}

function scoreCell(record) {
  const score = document.createElement("strong");
  score.className = record.scores.priority_band === "high" ? "score score--high" : "score";
  score.textContent = String(record.scores.priority);
  return score;
}

function renderRecords(records) {
  const ordered = [...records].sort((left, right) => right.scores.priority - left.scores.priority);
  const fragment = document.createDocumentFragment();

  for (const record of ordered) {
    const row = document.createElement("tr");
    const consumers = record.consumers.map((consumer) => consumer.name).join(", ");
    addCell(row, targetCell(record));
    addCell(row, stateCell(record));
    addCell(row, formatDate(record.sunset_at));
    addCell(row, consumers || "No mapped consumer");
    addCell(row, scoreCell(record));
    fragment.append(row);
  }

  recordsBody.replaceChildren(fragment);
  recordCount.textContent = String(records.length);
  highestPriority.textContent = String(
    Math.max(...records.map((record) => record.scores.priority)),
  );
}

async function loadAssessment() {
  try {
    const response = await fetch("demo/assessment.json", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const assessment = await response.json();
    if (!Array.isArray(assessment.records) || assessment.records.length === 0) {
      throw new Error("No lifecycle records");
    }
    renderRecords(assessment.records);
    loadState.textContent = "Verified from committed assessment.json.";
  } catch {
    loadState.textContent = "Showing the committed fallback snapshot.";
  }
}

loadAssessment();
