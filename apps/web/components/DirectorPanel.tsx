"use client";

/**
 * The AI Director panel.
 *
 * The chat is the primary editing surface, but every turn resolves to a
 * structured plan the user can read before it touches the timeline. Plans that
 * remove material or run long always wait for an explicit Apply.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { ChatMessage, EditPlan } from "@/lib/types";
import { getPlayhead, useEditor } from "@/store/editor";

const SUGGESTIONS = [
  "Make this cinematic",
  "Cut the first 3 seconds",
  "Slow the selected clip to 50%",
  "Remove the pauses",
  "Add the title \"NIGHT DRIVE\"",
  "Cross-dissolve between the clips",
  "Clean up my voice",
  "Make a vertical version",
];

export function DirectorPanel() {
  const projectId = useEditor((s) => s.projectId);
  const selection = useEditor((s) => s.selection);
  const patchDocument = useEditor((s) => s.patchDocument);
  const toast = useEditor((s) => s.toast);
  const llmAvailable = useEditor((s) => s.document !== null);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingPlans, setPendingPlans] = useState<Record<string, EditPlan>>({});
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!projectId) return;
    api.messages(projectId).then(setMessages).catch(() => undefined);
  }, [projectId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const send = useCallback(
    async (text: string) => {
      if (!projectId || !text.trim() || busy) return;
      const command = text.trim();
      setInput("");
      setBusy(true);

      const optimistic: ChatMessage = {
        id: `local-${Date.now()}`,
        role: "user",
        content: command,
        plan: null,
        applied: false,
        created_at: new Date().toISOString(),
      };
      setMessages((m) => [...m, optimistic]);

      try {
        const response = await api.chat(projectId, {
          message: command,
          selection,
          playhead: getPlayhead(),
          sequence_id: useEditor.getState().document?.active_sequence_id ?? null,
        });

        if (response.applied && response.document) {
          patchDocument(response.document);
          useEditor.setState({ canUndo: true, canRedo: false });
        } else if (response.plan.operations.length > 0) {
          setPendingPlans((p) => ({ ...p, [response.message_id]: response.plan }));
        }
        setMessages(await api.messages(projectId));
      } catch (err) {
        toast({
          kind: "error",
          title: "The director could not answer",
          detail: err instanceof Error ? err.message : undefined,
        });
        setMessages((m) => m.filter((x) => x.id !== optimistic.id));
      } finally {
        setBusy(false);
      }
    },
    [projectId, selection, busy, patchDocument, toast],
  );

  const applyPlan = async (messageId: string, plan: EditPlan) => {
    if (!projectId) return;
    setBusy(true);
    try {
      const result = await api.applyPlan(projectId, plan, {
        selection,
        playhead: getPlayhead(),
        sequence_id: useEditor.getState().document?.active_sequence_id ?? null,
        message_id: messageId,
      });
      if (result.ok) {
        patchDocument(result.document);
        useEditor.setState({ canUndo: true, canRedo: false });
        setPendingPlans((p) => {
          const next = { ...p };
          delete next[messageId];
          return next;
        });
        setMessages(await api.messages(projectId));
        toast({ kind: "success", title: "Edit applied" });
      } else {
        toast({
          kind: "error",
          title: "The plan could not be applied",
          detail: result.errors.join(" "),
        });
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col bg-ink-900">
      <div className="panel-header">
        <span>AI Director</span>
        <button
          className="btn btn-ghost h-6"
          onClick={async () => {
            if (!projectId) return;
            await api.clearMessages(projectId);
            setMessages([]);
            setPendingPlans({});
          }}
          title="Clear the conversation"
        >
          Clear
        </button>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {messages.length === 0 && (
          <div className="rounded border border-ink-800 bg-ink-850 p-3">
            <p className="text-xs leading-relaxed text-ink-300">
              Tell me what you want and I will turn it into real timeline
              operations — every one of them editable and undoable.
            </p>
            <div className="mt-2.5 flex flex-wrap gap-1">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => void send(s)}
                  className="chip transition-colors hover:border-accent/50 hover:text-ink-100"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((message) => (
          <MessageBubble
            key={message.id}
            message={message}
            pendingPlan={pendingPlans[message.id]}
            onApply={() => {
              const plan = pendingPlans[message.id];
              if (plan) void applyPlan(message.id, plan);
            }}
            onDismiss={() =>
              setPendingPlans((p) => {
                const next = { ...p };
                delete next[message.id];
                return next;
              })
            }
          />
        ))}

        {busy && (
          <div className="flex items-center gap-2 px-1 text-xs text-ink-400">
            <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
            Planning the edit…
          </div>
        )}
      </div>

      <div className="shrink-0 border-t border-ink-750 p-2.5">
        {selection.length > 0 && (
          <div className="mb-1.5 text-2xs text-ink-400">
            Scoped to {selection.length} selected clip
            {selection.length === 1 ? "" : "s"}
          </div>
        )}
        <div className="relative">
          <textarea
            ref={inputRef}
            rows={2}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send(input);
              }
            }}
            placeholder="Make the opening feel expensive…"
            className="w-full resize-none rounded border border-ink-700 bg-ink-850 px-2.5 py-2 pr-16 text-xs leading-relaxed text-ink-100 placeholder:text-ink-500 focus:border-accent/60 focus:outline-none"
          />
          <button
            className="btn btn-accent absolute bottom-2 right-2 h-6"
            onClick={() => void send(input)}
            disabled={busy || !input.trim()}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

function MessageBubble({
  message,
  pendingPlan,
  onApply,
  onDismiss,
}: {
  message: ChatMessage;
  pendingPlan?: EditPlan;
  onApply: () => void;
  onDismiss: () => void;
}) {
  if (message.role === "user") {
    return (
      <div className="ml-6 rounded rounded-br-none border border-ink-700 bg-ink-800 px-2.5 py-1.5">
        <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-100">
          {message.content}
        </p>
      </div>
    );
  }

  const plan = pendingPlan ?? message.plan;
  return (
    <div className="animate-in space-y-2">
      {message.content && (
        <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-200">
          {message.content}
        </p>
      )}
      {plan && plan.operations.length > 0 && (
        <PlanCard
          plan={plan}
          applied={message.applied}
          actionable={Boolean(pendingPlan)}
          onApply={onApply}
          onDismiss={onDismiss}
        />
      )}
    </div>
  );
}

const TIER_LABEL: Record<string, string> = {
  instant: "Instant",
  light: "Light",
  moderate: "Moderate",
  heavy: "Heavy",
};

function PlanCard({
  plan,
  applied,
  actionable,
  onApply,
  onDismiss,
}: {
  plan: EditPlan;
  applied: boolean;
  actionable: boolean;
  onApply: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="overflow-hidden rounded border border-ink-700 bg-ink-850">
      <div className="flex items-center justify-between border-b border-ink-750 px-2.5 py-1.5">
        <span className="text-2xs font-semibold uppercase tracking-[0.12em] text-ink-400">
          Edit plan
        </span>
        <span className="flex items-center gap-1.5">
          <span className="chip">{plan.planner === "llm" ? "model" : "rules"}</span>
          <span className="chip">{TIER_LABEL[plan.estimate.tier] ?? plan.estimate.tier}</span>
          {applied && <span className="chip text-audio">applied</span>}
        </span>
      </div>

      <ul className="divide-y divide-ink-800/70">
        {plan.operations.map((op, i) => (
          <li key={op.id ?? i} className="flex items-start gap-2 px-2.5 py-1.5">
            <span className="mt-[3px] text-audio">
              <IconCheck />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-xs leading-snug text-ink-200">
                {op.description || op.type.replace(/_/g, " ")}
              </span>
              <span className="font-mono text-2xs text-ink-500">{op.type}</span>
            </span>
          </li>
        ))}
      </ul>

      {plan.unsupported.length > 0 && (
        <div className="border-t border-ink-750 bg-accent/5 px-2.5 py-1.5 text-2xs text-accent">
          Not available yet:{" "}
          {plan.unsupported.map((u) => u.replace(/_/g, " ")).join(", ")}
        </div>
      )}

      {plan.rationale && (
        <div className="border-t border-ink-750 px-2.5 py-1.5 text-2xs leading-relaxed text-ink-400">
          {plan.rationale}
        </div>
      )}

      {actionable && (
        <div className="flex items-center gap-1.5 border-t border-ink-750 px-2.5 py-2">
          <button className="btn btn-accent h-6" onClick={onApply}>
            Apply edit
          </button>
          <button className="btn btn-ghost h-6" onClick={onDismiss}>
            Discard
          </button>
          <span className="ml-auto text-2xs text-ink-500">
            ~{plan.estimate.seconds}s to process
          </span>
        </div>
      )}
    </div>
  );
}

function IconCheck() {
  return (
    <svg width="10" height="10" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path d="M3 8.5 6.5 12 13 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}
