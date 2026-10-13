"use client";

/**
 * Editor state.
 *
 * Two rules shape this store:
 *
 * 1. The project document is the source of truth, and it has the same shape
 *    as the backend schema. Local edits mutate a copy and are committed as a
 *    version, so undo works identically whether a change came from a drag or
 *    from the AI Director.
 *
 * 2. Playback state is deliberately kept out of the React render path.
 *    `playhead` lives in a ref that the transport writes at 60fps; components
 *    that need it subscribe through `subscribePlayhead` and move a DOM node
 *    directly instead of re-rendering the timeline on every frame.
 */
import { create } from "zustand";

import { api, VersionConflictError } from "@/lib/api";
import type {
  Asset,
  Clip,
  ClipKind,
  EditOperation,
  ProjectDocument,
  Sequence,
  Track,
} from "@/lib/types";

export type Workspace =
  | "director" | "edit" | "color" | "audio" | "motion" | "assets" | "export";

interface Transient {
  playhead: number;
  playing: boolean;
}

const transient: Transient = { playhead: 0, playing: false };
const playheadListeners = new Set<(t: number) => void>();
const playStateListeners = new Set<(p: boolean) => void>();

export function getPlayhead() {
  return transient.playhead;
}

export function setPlayhead(time: number) {
  const clamped = Math.max(0, time);
  if (Math.abs(clamped - transient.playhead) < 0.0005) return;
  transient.playhead = clamped;
  playheadListeners.forEach((fn) => fn(clamped));
}

export function subscribePlayhead(fn: (t: number) => void) {
  playheadListeners.add(fn);
  fn(transient.playhead);
  return () => {
    playheadListeners.delete(fn);
  };
}

export function isPlaying() {
  return transient.playing;
}

export function setPlaying(value: boolean) {
  if (transient.playing === value) return;
  transient.playing = value;
  playStateListeners.forEach((fn) => fn(value));
}

export function subscribePlaying(fn: (p: boolean) => void) {
  playStateListeners.add(fn);
  fn(transient.playing);
  return () => {
    playStateListeners.delete(fn);
  };
}

export interface Toast {
  id: string;
  kind: "info" | "success" | "error" | "working";
  title: string;
  detail?: string;
}

interface EditorState {
  projectId: string | null;
  projectName: string;
  document: ProjectDocument | null;
  assets: Asset[];
  loading: boolean;
  saving: boolean;
  error: string | null;

  selection: string[];
  workspace: Workspace;
  zoom: number; // pixels per second
  snapping: boolean;
  canUndo: boolean;
  canRedo: boolean;
  /** Server head this client last saw. Sent with writes to catch a stale save. */
  version: number;
  toasts: Toast[];

  /** filename -> 0..1 upload progress, while a transfer is in flight. */
  uploads: Record<string, number>;

  // lifecycle
  load: (projectId: string) => Promise<void>;
  refreshAssets: () => Promise<void>;
  reload: () => Promise<void>;
  uploadFiles: (files: FileList | File[]) => Promise<void>;

  // selection
  select: (ids: string[]) => void;
  toggleSelect: (id: string, additive: boolean) => void;
  clearSelection: () => void;

  // view
  setWorkspace: (w: Workspace) => void;
  setZoom: (z: number) => void;
  toggleSnapping: () => void;

  // editing
  applyOps: (operations: EditOperation[], label?: string) => Promise<boolean>;
  patchDocument: (next: ProjectDocument) => void;
  commitDocument: (label: string) => Promise<void>;
  handleConflict: (error: VersionConflictError) => void;
  undo: () => Promise<void>;
  redo: () => Promise<void>;

  // helpers
  activeSequence: () => Sequence | null;
  clip: (id: string) => Clip | null;
  assetsById: () => Record<string, Asset>;

  toast: (t: Omit<Toast, "id">) => string;
  dismissToast: (id: string) => void;
}

function cloneDocument(doc: ProjectDocument): ProjectDocument {
  return typeof structuredClone === "function"
    ? structuredClone(doc)
    : (JSON.parse(JSON.stringify(doc)) as ProjectDocument);
}

export const useEditor = create<EditorState>((set, get) => ({
  projectId: null,
  projectName: "",
  document: null,
  assets: [],
  loading: true,
  saving: false,
  error: null,

  selection: [],
  workspace: "edit",
  zoom: 60,
  snapping: true,
  canUndo: false,
  canRedo: false,
  version: 0,
  toasts: [],
  uploads: {},

  async load(projectId) {
    set({ loading: true, error: null, projectId });
    try {
      const [project, assets] = await Promise.all([
        api.getProject(projectId),
        api.listAssets(projectId),
      ]);
      set({
        document: project.document,
        projectName: project.name,
        assets,
        version: project.version,
        canUndo: project.can_undo,
        canRedo: project.can_redo,
        loading: false,
      });
    } catch (err) {
      set({
        loading: false,
        error: err instanceof Error ? err.message : "Could not open this project.",
      });
    }
  },

  /** Re-read the document after a change made outside the edit engine. */
  async reload() {
    const { projectId } = get();
    if (!projectId) return;
    try {
      const project = await api.getProject(projectId);
      set({
        document: project.document,
        version: project.version,
        canUndo: project.can_undo,
        canRedo: project.can_redo,
      });
    } catch {
      /* leave the current document in place rather than blanking the editor */
    }
  },

  async refreshAssets() {
    const { projectId } = get();
    if (!projectId) return;
    try {
      set({ assets: await api.listAssets(projectId) });
    } catch {
      /* a transient asset-list failure should not break the editor */
    }
  },

  /**
   * Import files into the open project.
   *
   * This lives in the store rather than in the media panel because a drop can
   * land anywhere in the window, and every route to importing should behave
   * identically -- same progress, same errors, same refresh.
   */
  async uploadFiles(files) {
    const { projectId } = get();
    const list = Array.from(files);
    if (!projectId) {
      get().toast({
        kind: "error",
        title: "Open a project first",
        detail: "Media belongs to a project, so there is nowhere to put these yet.",
      });
      return;
    }
    if (list.length === 0) return;

    for (const file of list) {
      const key = `${file.name}-${file.size}`;
      set((s) => ({ uploads: { ...s.uploads, [key]: 0 } }));
      try {
        await api.uploadAsset(projectId, file, (fraction) =>
          set((s) => ({ uploads: { ...s.uploads, [key]: fraction } })),
        );
        await get().refreshAssets();
      } catch (err) {
        get().toast({
          kind: "error",
          title: `Could not import ${file.name}`,
          detail: err instanceof Error ? err.message : undefined,
        });
      } finally {
        set((s) => {
          const next = { ...s.uploads };
          delete next[key];
          return { uploads: next };
        });
      }
    }
  },

  select: (ids) => set({ selection: ids }),
  toggleSelect: (id, additive) =>
    set((s) => {
      if (!additive) return { selection: [id] };
      return s.selection.includes(id)
        ? { selection: s.selection.filter((x) => x !== id) }
        : { selection: [...s.selection, id] };
    }),
  clearSelection: () => set({ selection: [] }),

  setWorkspace: (workspace) => set({ workspace }),
  setZoom: (zoom) => set({ zoom: Math.max(4, Math.min(600, zoom)) }),
  toggleSnapping: () => set((s) => ({ snapping: !s.snapping })),

  async applyOps(operations, label = "Edit") {
    const { projectId, selection, document, version } = get();
    if (!projectId || !document) return false;
    set({ saving: true });
    try {
      const result = await api.applyOperations(projectId, operations, {
        selection,
        playhead: getPlayhead(),
        sequence_id: document.active_sequence_id,
        label,
        expectedVersion: version,
      });
      if (!result.ok) {
        get().toast({
          kind: "error",
          title: "That edit could not be applied",
          detail: result.errors.join(" "),
        });
        set({ saving: false });
        return false;
      }
      set({
        document: result.document,
        version: result.version,
        saving: false,
        canUndo: true,
        canRedo: false,
      });
      return true;
    } catch (err) {
      set({ saving: false });
      if (err instanceof VersionConflictError) {
        get().handleConflict(err);
        return false;
      }
      get().toast({
        kind: "error",
        title: "Edit failed",
        detail: err instanceof Error ? err.message : undefined,
      });
      return false;
    }
  },

  /**
   * Someone else edited this project first.
   *
   * The 409 carries the state that won, so the editor adopts it rather than
   * asking for a reload. The user's own change is lost, which is the honest
   * outcome -- so it is said plainly rather than hidden.
   */
  handleConflict(error) {
    set({
      document: error.detail.document,
      version: error.detail.current_version,
      canUndo: true,
      canRedo: false,
    });
    get().toast({
      kind: "error",
      title: "This project changed in another tab",
      detail:
        "Your last change was not saved. The editor now shows the newer " +
        "version, so try it again from here.",
    });
  },

  patchDocument: (next) => set({ document: next }),

  async commitDocument(label) {
    const { projectId, document } = get();
    if (!projectId || !document) return;
    set({ saving: true });
    try {
      const result = await api.saveDocument(
        projectId, document, label, get().version,
      );
      set({
        document: result.document,
        version: result.version,
        saving: false,
        canUndo: true,
        canRedo: false,
      });
    } catch (err) {
      set({ saving: false });
      if (err instanceof VersionConflictError) {
        get().handleConflict(err);
        return;
      }
      get().toast({
        kind: "error",
        title: "Could not save",
        detail: err instanceof Error ? err.message : undefined,
      });
    }
  },

  async undo() {
    const { projectId } = get();
    if (!projectId) return;
    const result = await api.undo(projectId);
    if (result.ok) {
      set({ document: result.document, canRedo: true });
      const project = await api.getProject(projectId);
      set({ canUndo: project.can_undo, canRedo: project.can_redo,
            version: project.version });
    } else {
      get().toast({ kind: "info", title: result.errors[0] ?? "Nothing to undo" });
    }
  },

  async redo() {
    const { projectId } = get();
    if (!projectId) return;
    const result = await api.redo(projectId);
    if (result.ok) {
      set({ document: result.document });
      const project = await api.getProject(projectId);
      set({ canUndo: project.can_undo, canRedo: project.can_redo,
            version: project.version });
    } else {
      get().toast({ kind: "info", title: result.errors[0] ?? "Nothing to redo" });
    }
  },

  activeSequence() {
    const doc = get().document;
    if (!doc) return null;
    return (
      doc.sequences.find((s) => s.id === doc.active_sequence_id) ??
      doc.sequences[0] ??
      null
    );
  },

  clip(id) {
    const seq = get().activeSequence();
    if (!seq) return null;
    for (const track of seq.tracks) {
      const found = track.clips.find((c) => c.id === id);
      if (found) return found;
    }
    return null;
  },

  assetsById() {
    return Object.fromEntries(get().assets.map((a) => [a.id, a]));
  },

  toast(t) {
    const id = Math.random().toString(36).slice(2, 10);
    set((s) => ({ toasts: [...s.toasts, { ...t, id }] }));
    if (t.kind !== "working") {
      setTimeout(() => get().dismissToast(id), t.kind === "error" ? 9000 : 4200);
    }
    return id;
  },

  dismissToast: (id) =>
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

// --- document helpers used by the timeline for optimistic edits -------------

export function withSequence(
  doc: ProjectDocument,
  mutate: (seq: Sequence) => void,
): ProjectDocument {
  const next = cloneDocument(doc);
  const seq =
    next.sequences.find((s) => s.id === next.active_sequence_id) ?? next.sequences[0];
  if (seq) mutate(seq);
  return next;
}

export function findClip(seq: Sequence, clipId: string): { track: Track; clip: Clip } | null {
  for (const track of seq.tracks) {
    const clip = track.clips.find((c) => c.id === clipId);
    if (clip) return { track, clip };
  }
  return null;
}

export function sequenceDuration(seq: Sequence | null): number {
  if (!seq) return 0;
  let end = 0;
  for (const track of seq.tracks) {
    for (const clip of track.clips) {
      if (clip.enabled) end = Math.max(end, clip.start + clip.duration);
    }
  }
  return end;
}

export function clipsAt(seq: Sequence, time: number, kinds?: ClipKind[]): Clip[] {
  const out: Clip[] = [];
  for (const track of seq.tracks) {
    if (track.hidden) continue;
    for (const clip of track.clips) {
      if (!clip.enabled) continue;
      if (kinds && !kinds.includes(clip.kind)) continue;
      if (time >= clip.start && time < clip.start + clip.duration) out.push(clip);
    }
  }
  return out;
}

/** Candidate snap targets: clip edges, markers, the playhead and zero. */
export function snapTargets(seq: Sequence, exclude: Set<string>): number[] {
  const points = [0];
  for (const track of seq.tracks) {
    for (const clip of track.clips) {
      if (exclude.has(clip.id)) continue;
      points.push(clip.start, clip.start + clip.duration);
    }
  }
  for (const marker of seq.markers) points.push(marker.time);
  points.push(getPlayhead());
  return points;
}

export function snap(value: number, targets: number[], toleranceSeconds: number): number {
  let best = value;
  let bestDelta = toleranceSeconds;
  for (const target of targets) {
    const delta = Math.abs(target - value);
    if (delta < bestDelta) {
      bestDelta = delta;
      best = target;
    }
  }
  return best;
}
