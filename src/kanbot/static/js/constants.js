export const COLUMNS = [
  { status: "follow_up", label: "Follow-up" },
  { status: "in_progress", label: "In progress" },
  { status: "blocked", label: "Blocked" },
  { status: "done", label: "Done" },
];

export const CANCELLED = { status: "cancelled", label: "Cancelled" };

export const PRIORITIES = [
  { value: "high", label: "High" },
  { value: "medium", label: "Medium" },
  { value: "low", label: "Low" },
];

// Keep in step with DESCRIPTION_TEMPLATE in api_docs.py.
export const DESCRIPTION_TEMPLATE = "Why: \nWhat: \nHow: ";

export const POLL_MS = 3000;

export const TOAST_MS = 4000;

export const CONFIRM_MS = 4000;

export const ALL_COLUMNS = [...COLUMNS, CANCELLED];
