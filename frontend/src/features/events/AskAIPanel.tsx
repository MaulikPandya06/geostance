import { useState } from "react";

type ResolutionType = {
  id: number;
  un_symbol: string;
  title: string;
  vote_date: string;
  body: string;
};

type Feature = {
  key: string;
  icon: string;
  label: string;
  description: string;
  isCustom?: boolean;
};

type Props = {
  selectedResolutions: ResolutionType[];
  onClose: () => void;
};

const FEATURES: Feature[] = [
  {
    key: "analyze",
    icon: "⊙",
    label: "Analyze the selected resolutions",
    description: "Summary, key points, and voting patterns",
  },
  {
    key: "compare",
    icon: "⇄",
    label: "Compare voting behavior",
    description: "See how countries voted differently",
  },
  {
    key: "blocs",
    icon: "◎",
    label: "Identify key blocs and alignments",
    description: "Discover voting blocs and regional patterns",
  },
  {
    key: "timeline",
    icon: "◷",
    label: "Track changes over time",
    description: "Analyze shifts between these resolutions",
  },
  {
    key: "themes",
    icon: "◈",
    label: "Extract key themes and topics",
    description: "What are the main issues discussed?",
  },
  {
    key: "custom",
    icon: "✦",
    label: "Custom analysis",
    description: "Specify countries and your own question",
    isCustom: true,
  },
];

function DownloadIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
    </svg>
  );
}

function Spinner() {
  return (
    <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
    </svg>
  );
}

export default function AskAIPanel({ selectedResolutions, onClose }: Props) {
  const [loadingFeature, setLoadingFeature] = useState<string | null>(null);
  const [question, setQuestion]             = useState("");
  const [countries, setCountries]           = useState("");
  const [error, setError]                   = useState<string | null>(null);

  const API_URL = import.meta.env.VITE_API_URL;
  const ids      = selectedResolutions.map((r) => r.id);

  const generate = async (feature: string) => {
    if (ids.length === 0) return;
    if (feature === "custom" && !question.trim() && !countries.trim()) {
      setError("Enter a question or specify countries for custom analysis.");
      return;
    }

    setError(null);
    setLoadingFeature(feature);

    const body: Record<string, unknown> = { resolution_ids: ids, feature };
    if (feature === "custom") {
      if (question.trim())  body.question  = question.trim();
      if (countries.trim()) body.countries = countries.split(",").map((c) => c.trim()).filter(Boolean);
    }

    try {
      const resp = await fetch(`${API_URL}/api/un-resolutions/generate-report/`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(body),
      });

      if (!resp.ok) {
        const msg = await resp.text();
        throw new Error(msg || `Server error ${resp.status}`);
      }

      // Trigger download
      const blob        = await resp.blob();
      const url         = URL.createObjectURL(blob);
      const a           = document.createElement("a");
      const disposition = resp.headers.get("Content-Disposition") ?? "";
      const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
      a.download        = nameMatch?.[1] ?? `GeoStance_${feature}_report.docx`;
      a.href            = url;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setError(err.message ?? "Generation failed — please retry.");
    } finally {
      setLoadingFeature(null);
    }
  };

  const isGenerating = loadingFeature !== null;

  return (
    <aside
      className="
        w-full sm:w-[400px] xl:w-[420px] shrink-0
        border-l border-gray-800/80 bg-[#030712]
        flex flex-col min-h-0
      "
    >
      {/* ── Header ── */}
      <div className="shrink-0 border-b border-gray-800/80 bg-gradient-to-b from-[#061120] to-[#030712] px-4 pt-4 pb-4">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
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
        <div className="flex flex-col gap-1.5 max-h-28 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {selectedResolutions.map((res) => (
            <div
              key={res.id}
              className="flex items-start gap-2 rounded-lg border border-gray-800 bg-gray-900/50 px-2.5 py-2"
            >
              <span className="font-mono text-[10px] font-semibold bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded shrink-0 mt-0.5">
                {res.un_symbol || `#${res.id}`}
              </span>
              <div className="min-w-0">
                <p className="text-xs text-gray-200 leading-snug truncate">
                  {res.title.length > 50 ? res.title.slice(0, 50) + "…" : res.title}
                </p>
                <p className="text-[10px] text-gray-500 mt-0.5">
                  {res.vote_date
                    ? new Date(res.vote_date).toLocaleDateString("en-US", {
                        year: "numeric", month: "short", day: "numeric",
                      })
                    : ""}
                  {res.body ? ` · ${res.body}` : ""}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* ── Scrollable body ── */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-2 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">

        <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider mb-3">
          Suggested prompts
        </p>

        {/* Feature buttons */}
        {FEATURES.filter((f) => !f.isCustom).map((feat) => {
          const isLoading = loadingFeature === feat.key;
          return (
            <button
              key={feat.key}
              disabled={isGenerating}
              onClick={() => generate(feat.key)}
              className="
                w-full flex items-center gap-3 px-3 py-3
                rounded-xl border border-gray-800 bg-gray-900/40
                hover:border-gray-700 hover:bg-gray-900/70
                disabled:opacity-40 disabled:cursor-not-allowed
                transition-all text-left group
              "
            >
              {/* Icon */}
              <div className="
                flex h-8 w-8 shrink-0 items-center justify-center
                rounded-lg border border-gray-700 bg-gray-800
                text-gray-400 text-sm
                group-hover:border-blue-500/40 group-hover:bg-blue-500/10 group-hover:text-blue-300
                transition-all
              ">
                {isLoading ? <Spinner /> : feat.icon}
              </div>

              {/* Text */}
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-gray-200 group-hover:text-white transition-colors leading-snug">
                  {feat.label}
                </p>
                <p className="text-[11px] text-gray-500 mt-0.5 leading-snug">
                  {feat.description}
                </p>
              </div>

              {/* Arrow / spinner */}
              <div className="shrink-0 text-gray-600 group-hover:text-gray-400 transition-colors">
                {isLoading
                  ? null
                  : (
                    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                  )}
              </div>
            </button>
          );
        })}

        {/* Custom section */}
        <div className="rounded-xl border border-gray-800 bg-gray-900/40 p-3 space-y-2.5 mt-1">
          <div className="flex items-center gap-2 mb-1">
            <span className="text-xs font-semibold text-gray-300">✦ Custom analysis</span>
            <span className="text-[10px] text-gray-500">— specify countries or a question</span>
          </div>

          <div>
            <label className="text-[10px] text-gray-500 uppercase tracking-wider mb-1 block">
              Countries (comma-separated, optional)
            </label>
            <input
              type="text"
              value={countries}
              onChange={(e) => setCountries(e.target.value)}
              placeholder="e.g. Russia, China, India, France"
              disabled={isGenerating}
              className="
                w-full rounded-lg border border-gray-700 bg-gray-800
                px-2.5 py-1.5 text-xs text-gray-200 placeholder-gray-600
                focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20
                disabled:opacity-40
              "
            />
          </div>

          <div>
            <label className="text-[10px] text-gray-500 uppercase tracking-wider mb-1 block">
              Question (optional)
            </label>
            <textarea
              rows={2}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="Ask anything about the selected resolutions…"
              disabled={isGenerating}
              className="
                w-full rounded-lg border border-gray-700 bg-gray-800
                px-2.5 py-1.5 text-xs text-gray-200 placeholder-gray-600 resize-none
                focus:outline-none focus:border-blue-500/50 focus:ring-1 focus:ring-blue-500/20
                disabled:opacity-40
              "
            />
          </div>

          <button
            onClick={() => generate("custom")}
            disabled={isGenerating}
            className="
              w-full flex items-center justify-center gap-2
              rounded-lg border border-blue-500/30 bg-blue-500/10
              px-3 py-2 text-xs font-semibold text-blue-300
              hover:bg-blue-500/20 hover:border-blue-500/50
              disabled:opacity-40 disabled:cursor-not-allowed
              transition-all
            "
          >
            {loadingFeature === "custom" ? <Spinner /> : <DownloadIcon />}
            {loadingFeature === "custom" ? "Generating report…" : "Generate custom report"}
          </button>
        </div>

        {/* Error */}
        {error && (
          <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-xs text-red-400">
            {error}
          </div>
        )}

        {/* Generating banner */}
        {isGenerating && (
          <div className="rounded-lg border border-blue-500/20 bg-blue-500/10 px-3 py-2.5 text-xs text-blue-300 flex items-center gap-2">
            <Spinner />
            <span>
              Generating report — this may take 30–60 seconds. The file will download automatically.
            </span>
          </div>
        )}
      </div>

      {/* ── Footer ── */}
      <div className="shrink-0 border-t border-gray-800 px-4 py-3">
        <p className="text-[10px] text-gray-600 text-center leading-relaxed">
          AI responses may contain inaccuracies. Verify critical information.
        </p>
      </div>
    </aside>
  );
}
