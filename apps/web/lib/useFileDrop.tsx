"use client";

/**
 * Window-wide file drop.
 *
 * Two jobs, and the first one matters more than it looks. A browser's default
 * response to a dropped file is to navigate to it, which throws away whatever
 * the user was doing and looks exactly like the app silently ignoring the
 * drop. So the guard is unconditional: every dragover and drop anywhere in the
 * document is cancelled, whether or not this app wants the file.
 *
 * The second job is accepting the drop wherever it lands, because aiming at a
 * narrow panel is not something anyone should have to do.
 */
import { useCallback, useEffect, useRef, useState } from "react";

export function useFileDrop(onFiles: (files: File[]) => void) {
  const [dragging, setDragging] = useState(false);
  // Drag events fire per element, so entering a child looks like leaving the
  // parent. Counting enter/leave pairs is what keeps the overlay steady.
  const depth = useRef(0);
  const handler = useRef(onFiles);
  handler.current = onFiles;

  const carriesFiles = useCallback((event: DragEvent) => {
    const types = event.dataTransfer?.types;
    return types ? Array.from(types).includes("Files") : false;
  }, []);

  useEffect(() => {
    const onDragEnter = (event: DragEvent) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      depth.current += 1;
      setDragging(true);
    };

    const onDragOver = (event: DragEvent) => {
      if (!carriesFiles(event)) return;
      // Without this the drop never fires and the browser navigates instead.
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    };

    const onDragLeave = (event: DragEvent) => {
      if (!carriesFiles(event)) return;
      depth.current = Math.max(0, depth.current - 1);
      if (depth.current === 0) setDragging(false);
    };

    const onDrop = (event: DragEvent) => {
      // Cancel unconditionally: a file dropped on any part of the page must
      // never replace the editor with a video player.
      event.preventDefault();
      depth.current = 0;
      setDragging(false);
      const files = Array.from(event.dataTransfer?.files ?? []);
      if (files.length > 0) handler.current(files);
    };

    window.addEventListener("dragenter", onDragEnter);
    window.addEventListener("dragover", onDragOver);
    window.addEventListener("dragleave", onDragLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onDragEnter);
      window.removeEventListener("dragover", onDragOver);
      window.removeEventListener("dragleave", onDragLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [carriesFiles]);

  return dragging;
}

/** Full-window drop target, shown only while files are over the page. */
export function DropOverlay({
  active,
  title,
  detail,
}: {
  active: boolean;
  title: string;
  detail: string;
}) {
  if (!active) return null;
  return (
    <div className="pointer-events-none fixed inset-0 z-[80] flex items-center justify-center bg-ink-950/75 p-10">
      <div className="animate-in flex w-full max-w-lg flex-col items-center rounded-lg border-2 border-dashed border-accent/70 bg-ink-900/90 px-8 py-12 text-center shadow-pop">
        <svg width="34" height="34" viewBox="0 0 24 24" fill="none" aria-hidden
             className="mb-3 text-accent">
          <path d="M12 16V4M12 4 7.5 8.5M12 4l4.5 4.5" stroke="currentColor"
                strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M3.5 15v3.5a1.5 1.5 0 0 0 1.5 1.5h14a1.5 1.5 0 0 0 1.5-1.5V15"
                stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
        <div className="text-sm font-medium text-ink-100">{title}</div>
        <div className="mt-1 text-xs text-ink-300">{detail}</div>
      </div>
    </div>
  );
}
