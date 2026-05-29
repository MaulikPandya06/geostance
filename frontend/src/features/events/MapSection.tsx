import WorldMap from "../map/WorldMap";
import MapLegend from "../map/MapLegend";
import type { ViewMode, ResolutionType } from "../../App";

type Props = {
  viewMode: ViewMode;
  heatmapData: any[];
  selectedEvent: any;
  setSelectedCountry: (country: any) => void;
  setCountryStatements: (statements: any[]) => void;
  voteMapData: Record<string, string>;
  selectedResolution: any;
  votesSummary: Record<string, number>;
  selectedResolutions: ResolutionType[];
  onClearSelection: () => void;
  onDownload: () => void;
  onAskAI: () => void;
};

const VOTE_HEADER = [
  { key: "yes",     label: "In Favour", color: "text-green-400"  },
  { key: "no",      label: "Against",   color: "text-red-400"    },
  { key: "abstain", label: "Abstain",   color: "text-yellow-400" },
  { key: "absent",  label: "Absent",    color: "text-gray-400"   },
] as const;

export default function MapSection({
  viewMode,
  heatmapData,
  selectedEvent,
  setSelectedCountry,
  setCountryStatements,
  voteMapData,
  selectedResolution,
  votesSummary,
  selectedResolutions,
  onClearSelection,
  onDownload,
  onAskAI,
}: Props) {
  const totalVotes = Object.values(votesSummary).reduce((s, n) => s + n, 0);

  return (
    <section className="flex-1 min-w-0 min-h-0 relative flex flex-col">

      {/* ── Resolution header (UN voting mode only) ── */}
      {viewMode === "un_voting" && selectedResolution && (
        <div className="shrink-0 border-b border-gray-800 bg-gray-950 px-4 py-3">
          <div className="flex flex-wrap items-start gap-x-4 gap-y-2">

            {/* Symbol + title */}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-1 flex-wrap">
                <span className="text-[11px] font-mono text-gray-500 uppercase tracking-wider">
                  Resolution
                </span>
                <span className="text-[11px] font-mono font-semibold bg-amber-500/20 text-amber-300 px-2 py-0.5 rounded">
                  {selectedResolution.un_symbol}
                </span>
              </div>
              <p className="text-sm font-semibold text-white leading-snug">
                {selectedResolution.title}
              </p>
              {selectedResolution.vote_date && (
                <p className="text-xs text-gray-500 mt-0.5">
                  {new Date(selectedResolution.vote_date).toLocaleDateString("en-US", {
                    year: "numeric", month: "long", day: "numeric",
                  })}
                </p>
              )}
            </div>

            {/* Vote totals */}
            {totalVotes > 0 && (
              <div className="flex items-center gap-4 shrink-0">
                {VOTE_HEADER.map(({ key, label, color }) =>
                  (votesSummary[key] ?? 0) > 0 ? (
                    <div key={key} className="text-center">
                      <p className={`text-lg font-bold leading-none ${color}`}>
                        {votesSummary[key]}
                      </p>
                      <p className="text-[10px] text-gray-500 mt-0.5 whitespace-nowrap">
                        {label}
                      </p>
                    </div>
                  ) : null
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Map ── */}
      <div className="flex-1 min-h-0 relative">
        <WorldMap
          viewMode={viewMode}
          heatmapData={heatmapData}
          selectedEvent={selectedEvent}
          setSelectedCountry={setSelectedCountry}
          setCountryStatements={setCountryStatements}
          voteMapData={voteMapData}
        />
        <MapLegend viewMode={viewMode} />
      </div>

      {/* ── Selection toolbar (UN voting mode only) ── */}
      {viewMode === "un_voting" && selectedResolutions.length > 0 && (
        <div className="shrink-0 border-t border-gray-800 bg-gray-950/95 backdrop-blur px-4 py-2.5 flex items-center gap-3">
          {/* Count */}
          <div className="flex items-center gap-1.5 text-xs font-semibold text-white">
            <div className="w-5 h-5 rounded bg-blue-500/20 border border-blue-500/30 flex items-center justify-center text-blue-300 text-[10px] font-bold">
              {selectedResolutions.length}
            </div>
            <span>selected</span>
          </div>

          <button
            onClick={onClearSelection}
            className="text-xs text-gray-400 hover:text-gray-200 transition-colors"
          >
            Clear
          </button>

          <div className="flex-1" />

          {/* Download */}
          <button
            onClick={onDownload}
            className="
              flex items-center gap-1.5 px-3 py-1.5 rounded-lg
              border border-gray-700 bg-gray-800/80
              text-xs text-gray-300 hover:text-white hover:border-gray-600 hover:bg-gray-700
              transition-all
            "
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
            </svg>
            Download
          </button>

          {/* Ask AI */}
          <button
            onClick={onAskAI}
            className="
              flex items-center gap-1.5 px-3 py-1.5 rounded-lg
              border border-blue-500/40 bg-blue-500/15
              text-xs font-semibold text-blue-300
              hover:bg-blue-500/25 hover:border-blue-500/60
              transition-all
            "
          >
            <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z" />
            </svg>
            Ask AI
          </button>
        </div>
      )}
    </section>
  );
}
