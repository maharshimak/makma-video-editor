/** Typed API client. Every network call in the editor goes through here. */
import type {
  Asset,
  Capabilities,
  ChatMessage,
  EditOperation,
  EditPlan,
  EffectSpec,
  HealthReport,
  Job,
  Project,
  ProjectDocument,
  ProjectSummary,
  Version,
} from "./types";

export interface ApplyResponse {
  ok: boolean;
  document: ProjectDocument;
  version_id: string | null;
  version: number;
  errors: string[];
  changed_clip_ids: string[];
  job_ids?: string[];
}

/** The body of a 409 from a write made against a version that has moved on. */
export interface VersionConflictDetail {
  code: "VERSION_CONFLICT";
  message: string;
  expected_version: number;
  current_version: number;
  document: ProjectDocument;
}

/**
 * A write refused because someone else edited first.
 *
 * Carries the state that won, so a caller can rebase rather than telling the
 * user to reload and lose their place.
 */
export class VersionConflictError extends Error {
  constructor(public detail: VersionConflictDetail) {
    super(detail.message);
    this.name = "VersionConflictError";
  }
}

export interface ChatResponse {
  message_id: string;
  reply: string;
  plan: EditPlan;
  applied: boolean;
  document: ProjectDocument | null;
  version_id: string | null;
  errors: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

const TOKEN_KEY = "makma.token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null) {
  if (typeof window === "undefined") return;
  if (token) window.localStorage.setItem(TOKEN_KEY, token);
  else window.localStorage.removeItem(TOKEN_KEY);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (!(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { ...init, headers });
  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }

  if (!response.ok) {
    const raw =
      body && typeof body === "object" && "detail" in body
        ? (body as { detail: unknown }).detail
        : null;

    // A version conflict is recoverable, so it gets its own type rather than
    // arriving as an opaque string the caller has to parse.
    if (
      response.status === 409 &&
      raw && typeof raw === "object" &&
      (raw as { code?: string }).code === "VERSION_CONFLICT"
    ) {
      throw new VersionConflictError(raw as VersionConflictDetail);
    }

    // A rejected edit comes back as {message, problems}. Showing the user
    // raw JSON would be a worse explanation than the one the server wrote.
    if (raw && typeof raw === "object" && "problems" in raw) {
      const structured = raw as { message?: string; problems?: string[] };
      const problems = (structured.problems ?? []).slice(0, 4).join("; ");
      throw new ApiError(
        response.status,
        [structured.message, problems].filter(Boolean).join(" ")
      );
    }

    const detail =
      (typeof raw === "string" ? raw : raw ? JSON.stringify(raw) : null) ??
      text ?? response.statusText;
    throw new ApiError(response.status, detail);
  }
  return body as T;
}

export const api = {
  // --- system ---
  capabilities: () => request<Capabilities>("/api/system/capabilities"),
  effects: () =>
    request<{ effects: EffectSpec[]; grade_presets: string[] }>("/api/system/effects"),

  // --- projects ---
  listProjects: () => request<ProjectSummary[]>("/api/projects"),
  createProject: (body: { name: string; width?: number; height?: number; fps?: number }) =>
    request<Project>("/api/projects", { method: "POST", body: JSON.stringify(body) }),
  getProject: (id: string) => request<Project>(`/api/projects/${id}`),
  renameProject: (id: string, name: string) =>
    request<Project>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  deleteProject: (id: string) =>
    request<void>(`/api/projects/${id}`, { method: "DELETE" }),

  saveDocument: (
    id: string,
    document: ProjectDocument,
    label = "Edit",
    expectedVersion?: number,
  ) =>
    request<ApplyResponse>(`/api/projects/${id}/document`, {
      method: "PUT",
      body: JSON.stringify({
        document, label, expected_version: expectedVersion ?? null,
      }),
    }),

  applyOperations: (
    id: string,
    operations: EditOperation[],
    opts: {
      selection?: string[]; playhead?: number; sequence_id?: string | null;
      label?: string; expectedVersion?: number;
    } = {},
  ) =>
    request<ApplyResponse>(`/api/projects/${id}/operations`, {
      method: "POST",
      body: JSON.stringify({
        operations,
        selection: opts.selection ?? [],
        playhead: opts.playhead ?? 0,
        sequence_id: opts.sequence_id ?? null,
        label: opts.label ?? "Edit",
        expected_version: opts.expectedVersion ?? null,
      }),
    }),

  undo: (id: string) =>
    request<ApplyResponse>(`/api/projects/${id}/undo`, { method: "POST" }),
  redo: (id: string) =>
    request<ApplyResponse>(`/api/projects/${id}/redo`, { method: "POST" }),
  versions: (id: string) => request<Version[]>(`/api/projects/${id}/versions`),
  restoreVersion: (id: string, versionId: string) =>
    request<ApplyResponse>(`/api/projects/${id}/versions/${versionId}/restore`, {
      method: "POST",
    }),

  // --- assets ---
  listAssets: (id: string) => request<Asset[]>(`/api/projects/${id}/assets`),
  getAsset: (id: string, assetId: string) =>
    request<Asset>(`/api/projects/${id}/assets/${assetId}`),
  assetUsage: (id: string, assetId: string) =>
    request<{ asset_id: string; name: string; clip_count: number }>(
      `/api/projects/${id}/assets/${assetId}/usage`,
    ),
  deleteAsset: (id: string, assetId: string, removeClips = false) =>
    request<void>(
      `/api/projects/${id}/assets/${assetId}?remove_clips=${removeClips}`,
      { method: "DELETE" },
    ),

  uploadAsset: (
    id: string,
    file: File,
    onProgress?: (fraction: number) => void,
  ): Promise<Asset> =>
    new Promise((resolve, reject) => {
      // XHR rather than fetch: upload progress is the whole point here.
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `/api/projects/${id}/assets`);
      const token = getToken();
      if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(JSON.parse(xhr.responseText) as Asset);
        } else {
          let detail = xhr.statusText;
          try {
            detail = JSON.parse(xhr.responseText).detail ?? detail;
          } catch {
            /* keep statusText */
          }
          reject(new ApiError(xhr.status, detail));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "The upload failed to reach the server."));
      xhr.send(form);
    }),

  // --- ai ---
  chat: (
    id: string,
    body: {
      message: string;
      selection?: string[];
      playhead?: number;
      sequence_id?: string | null;
      auto_apply?: boolean;
    },
  ) =>
    request<ChatResponse>(`/api/projects/${id}/ai/chat`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  applyPlan: (
    id: string,
    plan: EditPlan,
    opts: { selection?: string[]; playhead?: number; sequence_id?: string | null; message_id?: string } = {},
  ) =>
    request<ApplyResponse>(`/api/projects/${id}/ai/apply`, {
      method: "POST",
      body: JSON.stringify({
        plan,
        selection: opts.selection ?? [],
        playhead: opts.playhead ?? 0,
        sequence_id: opts.sequence_id ?? null,
        message_id: opts.message_id ?? null,
      }),
    }),
  messages: (id: string) => request<ChatMessage[]>(`/api/projects/${id}/ai/messages`),
  clearMessages: (id: string) =>
    request<void>(`/api/projects/${id}/ai/messages`, { method: "DELETE" }),

  // --- render ---
  render: (id: string, body: Record<string, unknown> = {}) =>
    request<Job>(`/api/projects/${id}/render`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  export: (id: string, body: Record<string, unknown> = {}) =>
    request<Job>(`/api/projects/${id}/export`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  health: (id: string, sequenceId?: string | null) =>
    request<HealthReport>(
      `/api/projects/${id}/health` +
      (sequenceId ? `?sequence_id=${sequenceId}` : ""),
    ),
  jobs: (id: string) => request<Job[]>(`/api/projects/${id}/jobs`),
  job: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),
  cancelJob: (jobId: string) =>
    request<Job>(`/api/jobs/${jobId}/cancel`, { method: "POST" }),
};

/**
 * Follow a job to completion over server-sent events, falling back to polling
 * if the stream drops. Returns an unsubscribe function.
 */
export function watchJob(
  jobId: string,
  onUpdate: (job: Job) => void,
  onDone?: (job: Job) => void,
): () => void {
  let closed = false;
  let poll: ReturnType<typeof setInterval> | null = null;

  const finish = (job: Job) => {
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      onDone?.(job);
      cleanup();
    }
  };

  const cleanup = () => {
    closed = true;
    source?.close();
    if (poll) clearInterval(poll);
  };

  const startPolling = () => {
    if (poll || closed) return;
    poll = setInterval(async () => {
      try {
        const job = await api.job(jobId);
        onUpdate(job);
        finish(job);
      } catch {
        cleanup();
      }
    }, 1200);
  };

  const source = new EventSource(`/api/jobs/${jobId}/stream`);
  source.onmessage = (event) => {
    try {
      const job = JSON.parse(event.data) as Job;
      onUpdate(job);
      finish(job);
    } catch {
      /* ignore a malformed frame; the next one usually lands */
    }
  };
  source.onerror = () => {
    source.close();
    startPolling();
  };

  return cleanup;
}
