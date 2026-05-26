const STORAGE_PREFIX = "student-wellness:";

const loginScreen = document.querySelector("#loginScreen");
const loginForm = document.querySelector("#loginForm");
const loginStudentId = document.querySelector("#loginStudentId");
const activeStudentId = document.querySelector("#activeStudentId");
const logoutButton = document.querySelector("#logoutButton");
const form = document.querySelector("#assessmentForm");
let currentStudentId = "";

const fields = {
  name: document.querySelector("#studentName"),
  stressQuestions: [...document.querySelectorAll(".stress-question")],
  sleep: document.querySelector("#sleep"),
  study: document.querySelector("#study"),
  totalClasses: document.querySelector("#totalClasses"),
  attendedClasses: document.querySelector("#attendedClasses"),
  notes: document.querySelector("#notes"),
};

const outputs = {
  stabilityScore: document.querySelector("#stabilityScore"),
  riskBand: document.querySelector("#riskBand"),
  studentTitle: document.querySelector("#studentTitle"),
  stressValue: document.querySelector("#stressValue"),
  sleepValue: document.querySelector("#sleepValue"),
  studyValue: document.querySelector("#studyValue"),
  attendanceRate: document.querySelector("#attendanceRate"),
  burnoutStatus: document.querySelector("#burnoutStatus"),
  stressStatus: document.querySelector("#stressStatus"),
  sleepStatus: document.querySelector("#sleepStatus"),
  attendanceStatus: document.querySelector("#attendanceStatus"),
  riskList: document.querySelector("#riskList"),
  suggestionList: document.querySelector("#suggestionList"),
  planList: document.querySelector("#planList"),
  alertBox: document.querySelector("#alertBox"),
};

function storageKey(studentId) {
  return `${STORAGE_PREFIX}${studentId}`;
}

function getFormData() {
  return {
    name: fields.name.value.trim(),
    stressAnswers: fields.stressQuestions.map((field) => field.value),
    sleep: fields.sleep.value,
    study: fields.study.value,
    totalClasses: fields.totalClasses.value,
    attendedClasses: fields.attendedClasses.value,
    notes: fields.notes.value,
    savedAt: new Date().toISOString(),
  };
}

function saveCurrentStudent() {
  if (!currentStudentId) {
    return;
  }

  localStorage.setItem(storageKey(currentStudentId), JSON.stringify(getFormData()));
}

function resetForm() {
  fields.name.value = "";
  fields.stressQuestions.forEach((field) => {
    field.value = "0";
  });
  fields.sleep.value = "7";
  fields.study.value = "4";
  fields.totalClasses.value = "";
  fields.attendedClasses.value = "";
  fields.notes.value = "";
}

function hasCheckInInput() {
  const stressRaw = fields.stressQuestions.reduce((sum, field) => sum + Number(field.value), 0);
  return Boolean(
    fields.name.value.trim()
      || fields.notes.value.trim()
      || fields.totalClasses.value
      || fields.attendedClasses.value
      || stressRaw > 0
      || fields.sleep.value !== "7"
      || fields.study.value !== "4"
  );
}

function loadStudent(studentId) {
  resetForm();
  const saved = localStorage.getItem(storageKey(studentId));

  if (saved) {
    const data = JSON.parse(saved);
    fields.name.value = data.name || "";
    fields.stressQuestions.forEach((field, index) => {
      field.value = data.stressAnswers?.[index] || "0";
    });
    fields.sleep.value = data.sleep || "5";
    fields.study.value = data.study || "9";
    fields.totalClasses.value = data.totalClasses || "";
    fields.attendedClasses.value = data.attendedClasses || "";
    fields.notes.value = data.notes || "";
  }

  currentStudentId = studentId;
  activeStudentId.textContent = studentId;
  loginScreen.classList.add("hidden");

  if (saved || hasCheckInInput()) {
    analyze();
  } else {
    setInitialState();
    outputs.studentTitle.textContent = `Student ${studentId}`;
  }
}

function calculateStress() {
  const total = fields.stressQuestions.reduce((sum, field) => sum + Number(field.value), 0);
  const estimatedStress = Math.round(1 + (total / 15) * 9);
  return {
    score: estimatedStress,
    raw: total,
  };
}

function calculateAttendance() {
  const total = Number(fields.totalClasses.value);
  const attended = Number(fields.attendedClasses.value);

  if (!total || total < 1) {
    return {
      status: "unknown",
      label: "--",
      percent: null,
      attended: 0,
      total: 0,
    };
  }

  const safeAttended = Math.max(0, Math.min(attended, total));
  const percent = Math.round((safeAttended / total) * 100);
  let status = "stable";

  if (percent < 75) {
    status = "critical";
  } else if (percent < 85) {
    status = "slipping";
  }

  return {
    status,
    label: `${safeAttended}/${total} classes (${percent}%)`,
    percent,
    attended: safeAttended,
    total,
  };
}

function listItems(target, items) {
  target.innerHTML = "";
  items.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    target.appendChild(li);
  });
}

function setInitialState() {
  outputs.stressValue.textContent = "Estimated stress: --/10";
  outputs.sleepValue.textContent = `${fields.sleep.value} hrs`;
  outputs.studyValue.textContent = `${fields.study.value} hrs`;
  outputs.attendanceRate.textContent = "Attendance: --";
  outputs.stabilityScore.textContent = "--";
  outputs.riskBand.className = "risk-band";
  outputs.riskBand.textContent = "Awaiting Check-In";
  outputs.studentTitle.textContent = "No student selected";
  outputs.burnoutStatus.textContent = "--";
  outputs.stressStatus.textContent = "--";
  outputs.sleepStatus.textContent = "--";
  outputs.attendanceStatus.textContent = "--";
  listItems(outputs.riskList, ["Complete the check-in to detect wellness and academic risk indicators."]);
  listItems(outputs.suggestionList, ["Suggestions will appear after analysis."]);
  listItems(outputs.planList, ["Recovery plan will appear after analysis."]);
  outputs.alertBox.classList.remove("urgent");
  outputs.alertBox.textContent = "Complete the check-in to generate counselor alert guidance.";
}

function analyze() {
  const stressResult = calculateStress();
  const stress = stressResult.score;
  const sleep = Number(fields.sleep.value);
  const study = Number(fields.study.value);
  const attendance = calculateAttendance();
  const notes = fields.notes.value.toLowerCase();

  outputs.stressValue.textContent = `Estimated stress: ${stress}/10`;
  outputs.sleepValue.textContent = `${sleep} hrs`;
  outputs.studyValue.textContent = `${study} hrs`;
  outputs.attendanceRate.textContent = `Attendance: ${attendance.label}`;
  outputs.studentTitle.textContent = fields.name.value.trim() || "Student";

  let risk = 0;
  const risks = [];
  const suggestions = [];
  const plan = [];

  if (stress >= 8) {
    risk += 28;
    risks.push("Stress overload: stress questionnaire indicates high pressure and reduced recovery capacity.");
    suggestions.push("Move one deadline or task to a lighter day and add a 20-minute decompression block.");
  } else if (stress >= 6) {
    risk += 18;
    risks.push("Stress is trending above a healthy baseline.");
    suggestions.push("Use a short prioritization session to separate urgent work from low-value tasks.");
  }

  if (sleep < 6) {
    risk += 24;
    risks.push("Sleep imbalance: recovery time is below the recommended student baseline.");
    suggestions.push("Set a fixed sleep window tonight and stop study work 45 minutes before bed.");
  } else if (sleep < 7) {
    risk += 12;
    risks.push("Sleep is slightly low and may reduce memory retention.");
  }

  if (study >= 10) {
    risk += 22;
    risks.push("Unhealthy study pattern: long study load may increase burnout risk.");
    suggestions.push("Split study blocks into 50-minute sessions with real breaks and one no-study meal.");
  } else if (study >= 8) {
    risk += 12;
    risks.push("Study load is heavy and needs balancing.");
  }

  if (attendance.status === "critical") {
    risk += 24;
    risks.push(`Attendance decline has reached escalation level: ${attendance.label}.`);
    suggestions.push("Notify mentor or counselor and check whether missed classes are avoidable or health-related.");
  } else if (attendance.status === "slipping") {
    risk += 14;
    risks.push(`Attendance decline is emerging: ${attendance.label}.`);
    suggestions.push("Schedule a low-pressure check-in and identify the classes most at risk.");
  }

  if (notes.includes("drained") || notes.includes("hopeless") || notes.includes("panic") || notes.includes("alone")) {
    risk += 18;
    risks.push("Emotional risk indicator detected in mood notes.");
    suggestions.push("Offer a supportive check-in and provide access to campus wellness resources.");
  }

  if (!risks.length) {
    risks.push("No major risk indicators detected today.");
    suggestions.push("Maintain current routine and repeat the check-in tomorrow.");
  }

  plan.push("Pick the top two academic tasks and postpone or delegate nonessential work.");
  plan.push("Protect sleep first, then schedule study in shorter focused blocks.");
  plan.push("Review attendance and backlog with a mentor within the next school day.");

  const stability = Math.max(0, Math.min(100, 100 - risk));
  outputs.stabilityScore.textContent = stability;

  outputs.riskBand.className = "risk-band";
  if (stability >= 75) {
    outputs.riskBand.textContent = "Stable";
    outputs.riskBand.classList.add("low");
  } else if (stability >= 45) {
    outputs.riskBand.textContent = "Moderate Watch";
    outputs.riskBand.classList.add("moderate");
  } else {
    outputs.riskBand.textContent = "High Risk";
    outputs.riskBand.classList.add("high");
  }

  outputs.burnoutStatus.textContent = study >= 8 && stress >= 6 ? "Elevated" : "Controlled";
  outputs.stressStatus.textContent = stress >= 8 ? "High" : stress >= 6 ? "Moderate" : "Low";
  outputs.sleepStatus.textContent = sleep < 6 ? "Imbalanced" : sleep < 7 ? "Watch" : "Healthy";
  outputs.attendanceStatus.textContent =
    attendance.status === "unknown"
      ? "--"
      : attendance.status === "critical"
        ? `Critical (${attendance.percent}%)`
        : attendance.status === "slipping"
          ? `Watch (${attendance.percent}%)`
          : `Stable (${attendance.percent}%)`;

  listItems(outputs.riskList, risks);
  listItems(outputs.suggestionList, suggestions);
  listItems(outputs.planList, plan);

  outputs.alertBox.classList.toggle("urgent", stability < 45);
  outputs.alertBox.textContent =
    stability < 45
      ? "Counselor alert: immediate support check-in recommended."
      : "Counselor alert: monitor for 48 hours before escalation.";

  saveCurrentStudent();
}

loginForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const studentId = loginStudentId.value.trim().toUpperCase();

  if (!studentId) {
    return;
  }

  loadStudent(studentId);
});

logoutButton.addEventListener("click", () => {
  saveCurrentStudent();
  currentStudentId = "";
  loginStudentId.value = "";
  activeStudentId.textContent = "--";
  loginScreen.classList.remove("hidden");
  loginStudentId.focus();
});

form.addEventListener("submit", (event) => {
  event.preventDefault();
  analyze();
});

const inputFields = [
  fields.name,
  ...fields.stressQuestions,
  fields.sleep,
  fields.study,
  fields.totalClasses,
  fields.attendedClasses,
  fields.notes,
];

inputFields.forEach((field) => {
  field.addEventListener("input", () => {
    if (hasCheckInInput()) {
      analyze();
    } else {
      setInitialState();
      saveCurrentStudent();
    }
  });
});

setInitialState();
