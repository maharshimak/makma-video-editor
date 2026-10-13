"use client";

import { useEditor } from "@/store/editor";

const STYLES: Record<string, string> = {
  info: "border-ink-600 bg-ink-800 text-ink-100",
  success: "border-audio/50 bg-audio/10 text-ink-100",
  error: "border-danger/50 bg-danger/10 text-ink-100",
  working: "border-accent/50 bg-accent/10 text-ink-100",
};

export function Toasts() {
  const toasts = useEditor((s) => s.toasts);
  const dismiss = useEditor((s) => s.dismissToast);

  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`animate-in pointer-events-auto rounded border px-3 py-2 shadow-pop ${STYLES[t.kind]}`}
        >
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium">{t.title}</div>
              {t.detail && (
                <div className="mt-0.5 break-words text-2xs leading-relaxed text-ink-300">
                  {t.detail}
                </div>
              )}
            </div>
            <button
              className="shrink-0 text-ink-400 hover:text-ink-100"
              onClick={() => dismiss(t.id)}
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
