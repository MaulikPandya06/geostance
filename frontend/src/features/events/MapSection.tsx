import WorldMap from "../map/WorldMap";
import MapLegend from "../map/MapLegend";
import type { ViewMode } from "../../App";

type Props = {
  viewMode: ViewMode;
  heatmapData: any[];
  selectedEvent: any;
  setSelectedCountry: (country: any) => void;
  setCountryStatements: (statements: any[]) => void;
  voteMapData: Record<string, string>;
  selectedResolution: any;
  votesSummary: Record<string, number>;
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
    </section>
  );
}
