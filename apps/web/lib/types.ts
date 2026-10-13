/**
 * Mirrors `makma.schema.core` and `makma.schema.operations`.
 *
 * The project document is the shared contract between the Python engines and
 * this editor; keeping the shapes identical means the timeline the user drags
 * is literally the timeline that gets rendered.
 */

export type TrackKind = "video" | "audio";
export type ClipKind = "video" | "audio" | "image" | "text" | "solid" | "adjustment";
export type FitMode = "contain" | "cover" | "stretch" | "none";
export type TransitionType =
  | "none" | "fade" | "dissolve"
  | "wipe_left" | "wipe_right"
  | "slide_left" | "slide_right" | "slide_up" | "slide_down";
export type Easing = "linear" | "ease_in" | "ease_out" | "ease_in_out" | "hold";

export interface Keyframe {
  id: string;
  time: number;
  value: number;
  easing: Easing;
}

export interface Transform {
  x: number;
  y: number;
  scale: number;
  rotation: number;
  opacity: number;
  fit: FitMode;
  crop_left: number;
  crop_right: number;
  crop_top: number;
  crop_bottom: number;
  /** Property name -> curve, in clip-local seconds. */
  keyframes: Record<string, Keyframe[]>;
}

/**
 * Transform properties that animate all the way through to the render.
 *
 * `scale` and `rotation` are deliberately absent: FFmpeg fixes a layer's size
 * when the filter starts and reads a rotation angle once, so animating them
 * would look right here and come out static in the export. Use the Push In
 * effect for an animated zoom.
 */
export const ANIMATABLE_TRANSFORM = ["x", "y", "opacity"] as const;
export type AnimatableTransform = (typeof ANIMATABLE_TRANSFORM)[number];

export interface Effect {
  id: string;
  type: string;
  enabled: boolean;
  parameters: Record<string, unknown>;
  keyframes: Record<string, Keyframe[]>;
  label?: string | null;
  created_by?: string | null;
}

export interface Transition {
  type: TransitionType;
  duration: number;
}

export interface TextStyle {
  font_family: string;
  font_size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  align: "left" | "center" | "right";
  letter_spacing: number;
  line_height: number;
  uppercase: boolean;
  stroke_color: string | null;
  stroke_width: number;
  shadow: boolean;
  background: string | null;
  background_padding: number;
}

export interface TextContent {
  text: string;
  style: TextStyle;
  anchor_x: number;
  anchor_y: number;
  animation: string;
}

export interface Clip {
  id: string;
  kind: ClipKind;
  name: string;
  asset_id: string | null;
  start: number;
  duration: number;
  in_point: number;
  speed: number;
  reverse: boolean;
  volume: number;
  muted: boolean;
  transform: Transform;
  effects: Effect[];
  transition_in: Transition;
  transition_out: Transition;
  text: TextContent | null;
  color: string | null;
  enabled: boolean;
  locked: boolean;
  label_color: string | null;
  meta: Record<string, unknown>;
}

export interface Track {
  id: string;
  kind: TrackKind;
  name: string;
  index: number;
  clips: Clip[];
  muted: boolean;
  hidden: boolean;
  locked: boolean;
  solo: boolean;
  volume: number;
  is_adjustment: boolean;
}

export interface Marker {
  id: string;
  time: number;
  name: string;
  color: string;
  kind: string;
}

export interface Sequence {
  id: string;
  name: string;
  width: number;
  height: number;
  fps: number;
  sample_rate: number;
  background: string;
  tracks: Track[];
  markers: Marker[];
}

export interface ProjectDocument {
  schema_version: number;
  id: string;
  name: string;
  sequences: Sequence[];
  active_sequence_id: string | null;
  styles: unknown[];
  meta: Record<string, unknown>;
}

export interface ProjectSummary {
  id: string;
  name: string;
  description: string;
  created_at: string;
  updated_at: string;
  duration: number;
  asset_count: number;
  thumbnail_url: string | null;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  document: ProjectDocument;
  created_at: string;
  updated_at: string;
  /** Monotonic head. Sent back with a write so a stale save is refused. */
  version: number;
  can_undo: boolean;
  can_redo: boolean;
}

export interface SceneInfo {
  id: string;
  index: number;
  start: number;
  end: number;
  thumbnail: string | null;
  quality_score: number;
  energy_score: number;
  brightness: number;
  motion_score: number;
}

export interface Analysis {
  scenes: SceneInfo[];
  silences: { start: number; end: number }[];
  highlights: { start: number; end: number; score: number; reason: string }[];
  loudness_lufs: number | null;
}

export interface Asset {
  id: string;
  project_id: string;
  name: string;
  kind: "video" | "audio" | "image" | "lut" | "font" | "other";
  status: "uploading" | "ingesting" | "ready" | "failed";
  error: string;
  duration: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
  size_bytes: number;
  created_at: string;
  proxy_url: string | null;
  thumbnail_url: string | null;
  filmstrip_url: string | null;
  waveform_url: string | null;
  original_url: string | null;
  scene_count: number;
  analysis: Analysis | null;
}

// --- operations ------------------------------------------------------------

export interface Scope {
  type: "clip" | "track" | "range" | "selection" | "all" | "sequence";
  clip_ids?: string[];
  track_ids?: string[];
  start?: number | null;
  end?: number | null;
  kinds?: string[];
}

export interface EditOperation {
  id?: string;
  type: string;
  scope?: Scope;
  parameters?: Record<string, unknown>;
  description?: string;
}

export interface CostEstimate {
  tier: "instant" | "light" | "moderate" | "heavy";
  seconds: number;
  gpu_seconds: number;
  usd: number;
  notes: string;
}

export interface EditPlan {
  id: string;
  command: string;
  summary: string;
  rationale: string;
  operations: EditOperation[];
  estimate: CostEstimate;
  unsupported: string[];
  requires_confirmation: boolean;
  planner: string;
  warnings: string[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  plan: EditPlan | null;
  applied: boolean;
  created_at: string;
}

export interface Job {
  id: string;
  type: string;
  status:
    | "queued" | "planning" | "processing" | "rendering"
    | "quality_check" | "completed" | "failed" | "cancelled";
  stage: string;
  progress: number;
  project_id: string | null;
  result: {
    url?: string;
    storage_key?: string;
    filename?: string;
    duration?: number;
    size_bytes?: number;
    seconds_taken?: number;
    resolution?: string;
    warnings?: string[];
    estimate?: Record<string, unknown>;
  };
  error: string;
  log: { t: string; message: string }[];
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Version {
  id: string;
  seq: number;
  label: string;
  source: string;
  created_at: string;
  operation_count: number;
  is_current: boolean;
}

export interface HealthIssue {
  severity: "error" | "warning" | "info";
  code: string;
  message: string;
  time: number | null;
  clip_id: string | null;
  sequence_id: string | null;
  fix: string;
}

export interface HealthReport {
  ready_to_export: boolean;
  duration: number;
  clip_count: number;
  counts: { error: number; warning: number; info: number };
  issues: HealthIssue[];
}

export interface EffectSpec {
  type: string;
  label: string;
  category: string;
  defaults: Record<string, unknown>;
  animatable: string[];
}

export interface Capabilities {
  app: string;
  environment: string;
  media: {
    ffmpeg: { available: boolean; version: string; path: string };
    proxy_height: number;
    max_upload_mb: number;
  };
  ai: {
    planner_mode: string;
    llm_available: boolean;
    router: unknown;
    note: string;
  };
  editing: {
    operations: string[];
    implemented_operations: string[];
    transitions: TransitionType[];
    grade_presets: string[];
    fonts: string[];
  };
  export_presets: {
    key: string; label: string; width: number; height: number;
    fps: number; note: string;
  }[];
  planned: { key: string; label: string; milestone: string }[];
}
