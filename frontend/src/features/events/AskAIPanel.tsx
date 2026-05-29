import { useEffect, useRef, useState } from "react";
import type { ResolutionType } from "../../App";

// ── Types ─────────────────────────────────────────────────────────────────────

type Message = {
  id:       string;
  role:     "user" | "ai";
  content:  string;
  loading?: boolean;
};

type Feature = {
  key:         string;
  label:       string;
  description: string;
  icon:        React.ReactNode;
};

type Props = {
  selectedResolutions: ResolutionType[];
  onClose: () => void;
};

// ── Feature definitions ───────────────────────────────────────────────────────

const FEATURES: Feature[] = [
  {
    key: "analyze",
    label: "Analyze the selected resolutions",
    description: "Summary, key points, and voting patterns",
    icon: (
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
        <circle cx="11" cy="11" r="7" strokeLinecap="round" />
        <path strokeLinecap="round" d="M21 21l-4.35-4.35M11 8v6M8 11h6" />
      </svg>
    ),
  },
  {
    key: "compare",
    label: "Compare voting behavior",
    description: "See how countries voted differently",
    icon: (
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M7 16V4m0 0L3 8m4-4l4 4M17 8v12m0 0l4-4m-4 4l-4-4" />
      </svg>
    ),
  },
  {
    key: "blocs",
    label: "Identify key blocs and alignments",
    description: "Discover voting blocs and regional patterns",
    icon: (
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
        <circle cx="12" cy="12" r="9" />
        <circle cx="12" cy="12" r="3" />
        <path strokeLinecap="round" d="M12 3v3M12 18v3M3 12h3M18 12h3" />
      </svg>
    ),
  },
  {
    key: "timeline",
    label: "Track changes over time",
    description: "Analyze shifts between these resolutions",
    icon: (
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
        <circle cx="12" cy="12" r="9" />
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 7v5l3 3" />
      </svg>
    ),
  },
  {
    key: "themes",
    label: "Extract key themes and topics",
    description: "What are the main issues discussed?",
    icon: (
      <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.8}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M7 7h10M7 11h6M7 15h8" />
        <rect x="3" y="3" width="18" height="18" rx="2" />
      </svg>
    ),
  },
];

// ── Small helpers ─────────────────────────────────────────────────────────────

function SparklesIcon() {
  return (
    <svg className="w-4 h-4 text-blue-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
    </svg>
  );
}

function LoadingDots() {
  return (
    <div className="flex items-center gap-1 py-1">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-blue-400 animate-bounce"
          style={{ animationDelay: `${i * 0.15}s` }}
        />
      ))}
    </div>
  );
}

// ── Simple markdown renderer ───────────────────────────────────────────────────
// Handles **bold**, bullet lines, numbered lines, and paragraph breaks.

function MarkdownContent({ text }: { text: string }) {
  const lines = text.split("\n");

  return (
    <div className="space-y-1">
      {lines.map((line, i) => {
        if (line.trim() === "") return <div key={i} className="h-2" />;

        // Render inline bold spans
        const parts = line.split(/(\*\*[^*]+\*\*)/g);
        const rendered = parts.map((part, j) =>
          part.startsWith("**") && part.endsWith("**") ? (
            <strong key={j} className="font-semibold text-white">
              {part.slice(2, -2)}
            </strong>
          ) : (
            <span key={j}>{part}</span>
          )
        );

        return (
          <p key={i} className="text-xs text-gray-200 leading-relaxed">
            {rendered}
          </p>
        );
      })}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function AskAIPanel({ selectedResolutions, onClose }: Props) {
  const [messages, setMessages]   = useState<Message[]>([]);
  const [input, setInput]         = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef            = useRef<HTMLDivElement>(null);
  const inputRef                  = useRef<HTMLTextAreaElement>(null);
  const API_URL = import.meta.env.VITE_API_URL;
  const ids     = selectedResolutions.map((r) => r.id);

  // Auto-scroll to latest message
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const sendMessage = async (feature: string, displayText: string) => {
    if (isLoading || ids.length === 0) return;

    const userMsgId = `u-${Date.now()}`;
    const aiMsgId   = `a-${Date.now()}`;

    setMessages((prev) => [
      ...prev,
      { id: userMsgId, role: "user",  content: displayText },
      { id: aiMsgId,   role: "ai",    content: "", loading: true },
    ]);
    setIsLoading(true);
    setInput("");

    try {
      const body: Record<string, unknown> = { resolution_ids: ids, feature };
      if (feature === "custom") body.question = displayText;

      const resp = await fetch(`${API_URL}/api/un-resolutions/ask/`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });

      if (!resp.ok) {
        const msg = await resp.text();
        throw new Error(msg || `Server error ${resp.status}`);
      }

      const data = await resp.json();
      setMessages((prev) =>
        prev.map((m) =>
          m.id === aiMsgId ? { ...m, content: data.answer, loading: false } : m
        )
      );
    } catch (err: any) {
      setMessages((prev) =>
        prev.map((m) =>
          m.id === aiMsgId
            ? { ...m, content: `Error: ${err.message ?? "Request failed — please retry."}`, loading: false }
            : m
        )
      );
    } finally {
      setIsLoading(false);
      inputRef.current?.focus();
    }
  };

  const handleSend = () => {
    const text = input.trim();
    if (!text || isLoading) return;
    sendMessage("custom", text);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const showPrompts = messages.length === 0;

  return (
    <aside
      className="
        w-full sm:w-[420px] xl:w-[440px] shrink-0
        border-l border-gray-800/80 bg-[#030712]
        flex flex-col min-h-0
      "
    >
      {/* ── Header ── */}
      <div className="shrink-0 border-b border-gray-800/80 bg-gradient-to-b from-[#061120] to-[#030712] px-4 pt-4 pb-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <SparklesIcon />
            <span className="text-[13px] font-semibold text-white tracking-tight">
              Ask GeoStance AI
            </span>
            <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-blue-500/20 text-blue-300 border border-blue-500/20 uppercase tracking-wider">
              Beta
            </span>
          </div>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-300 transition-colors p-1 rounded-lg hover:bg-gray-800"
            aria-label="Close"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <p className="text-xs text-gray-500 mb-3">
          You have selected{" "}
          <span className="text-white font-semibold">{ids.length}</span>{" "}
          resolution{ids.length !== 1 ? "s" : ""}
        </p>

        {/* Selected resolution cards */}
        <div className="flex gap-2 flex-wrap max-h-24 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {selectedResolutions.map((res) => (
            <div
              key={res.id}
              className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-900/50 px-2.5 py-2 min-w-0 flex-1 basis-[46%]"
            >
              <span className="font-mono text-[10px] font-semibold bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded shrink-0 mt-0.5">
                {res.un_symbol || `#${res.id}`}
              </span>
              <div className="min-w-0">
                <p className="text-[11px] text-gray-200 leading-snug truncate">
                  {res.title.length > 40 ? res.title.slice(0, 40) + "…" : res.title}
                </p>
                <p className="text-[10px] text-gray-500 mt-0.5">
                  {res.vote_date
                    ? new Date(res.vote_date).toLocaleDateString("en-US", {
                        year: "numeric", month: "short", day: "numeric",
                      })
                    : ""}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Body ── */}
      {showPrompts ? (
        /* Suggested prompts — shown before any messages */
        <div className="flex-1 overflow-y-auto px-4 py-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider mb-3">
            Suggested prompts
          </p>

          <div className="space-y-2">
            {FEATURES.map((feat) => (
              <button
                key={feat.key}
                disabled={isLoading}
                onClick={() => sendMessage(feat.key, feat.label)}
                className="
                  w-full flex items-center gap-3 px-3 py-3
                  rounded-xl border border-gray-800 bg-gray-900/40
                  hover:border-gray-700 hover:bg-gray-900/70
                  disabled:opacity-40 disabled:cursor-not-allowed
                  transition-all text-left group
                "
              >
                <div className="
                  flex h-8 w-8 shrink-0 items-center justify-center
                  rounded-lg border border-gray-700 bg-gray-800 text-gray-400
                  group-hover:border-blue-500/40 group-hover:bg-blue-500/10 group-hover:text-blue-300
                  transition-all
                ">
                  {feat.icon}
                </div>

                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold text-gray-200 group-hover:text-white transition-colors leading-snug">
                    {feat.label}
                  </p>
                  <p className="text-[11px] text-gray-500 mt-0.5 leading-snug">
                    {feat.description}
                  </p>
                </div>

                <svg
                  className="w-4 h-4 shrink-0 text-gray-600 group-hover:text-gray-400 transition-colors"
                  fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                </svg>
              </button>
            ))}
          </div>
        </div>
      ) : (
        /* Chat thread — shown after first message */
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {messages.map((msg) =>
            msg.role === "user" ? (
              <div key={msg.id} className="flex justify-end">
                <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-blue-600/25 border border-blue-500/20 px-3 py-2.5">
                  <p className="text-xs text-blue-100 leading-relaxed">{msg.content}</p>
                </div>
              </div>
            ) : (
              <div key={msg.id} className="flex gap-2.5">
                {/* AI avatar */}
                <div className="mt-0.5 w-6 h-6 rounded-full bg-gradient-to-br from-blue-500/30 to-purple-500/20 border border-blue-500/20 shrink-0 flex items-center justify-center">
                  <SparklesIcon />
                </div>
                <div className="flex-1 min-w-0 rounded-2xl rounded-tl-sm border border-gray-800 bg-gray-900/60 px-3 py-2.5">
                  {msg.loading ? (
                    <LoadingDots />
                  ) : (
                    <MarkdownContent text={msg.content} />
                  )}
                </div>
              </div>
            )
          )}
          <div ref={messagesEndRef} />
        </div>
      )}

      {/* ── Input footer ── */}
      <div className="shrink-0 border-t border-gray-800 px-4 pt-3 pb-3">
        {/* Compact prompt chips when in chat mode */}
        {!showPrompts && (
          <div className="flex gap-1.5 flex-wrap mb-3">
            {FEATURES.map((feat) => (
              <button
                key={feat.key}
                disabled={isLoading}
                onClick={() => sendMessage(feat.key, feat.label)}
                className="
                  flex items-center gap-1 px-2 py-1 rounded-lg
                  border border-gray-800 bg-gray-900/50 text-[10px] text-gray-400
                  hover:border-gray-700 hover:text-gray-200 hover:bg-gray-800
                  disabled:opacity-40 disabled:cursor-not-allowed
                  transition-all
                "
              >
                <span className="text-gray-500">{feat.icon}</span>
                {feat.label.split(" ").slice(0, 2).join(" ")}
              </button>
            ))}
          </div>
        )}

        {/* Text input row */}
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              // Auto-grow up to 4 rows
              e.target.style.height = "auto";
              e.target.style.height = Math.min(e.target.scrollHeight, 96) + "px";
            }}
            onKeyDown={handleKeyDown}
            placeholder="Ask anything about the selected resolutions…"
            disabled={isLoading}
            className="
              flex-1 resize-none rounded-xl border border-gray-700 bg-gray-800/80
              px-3 py-2.5 text-xs text-gray-200 placeholder-gray-600
              focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20
              disabled:opacity-50 leading-relaxed
              overflow-hidden
            "
            style={{ minHeight: "38px", maxHeight: "96px" }}
          />
          <button
            onClick={handleSend}
            disabled={isLoading || !input.trim()}
            className="
              shrink-0 w-9 h-9 rounded-xl flex items-center justify-center
              bg-blue-600 text-white
              hover:bg-blue-500 disabled:opacity-40 disabled:cursor-not-allowed
              transition-all
            "
            aria-label="Send"
          >
            {isLoading ? (
              <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
            ) : (
              <SendIcon />
            )}
          </button>
        </div>

        <p className="text-[10px] text-gray-600 text-center mt-2.5 leading-relaxed">
          AI responses may contain inaccuracies. Verify critical information.
        </p>
      </div>
    </aside>
  );
}
