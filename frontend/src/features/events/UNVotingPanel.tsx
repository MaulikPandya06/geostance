import { useState } from "react";

type VoteCategory = "yes" | "no" | "abstain" | "absent";

type Props = {
  selectedResolution: any;
  votesSummary: Record<string, number>;
  votesByCategory: Record<string, any[]>;
  selectedCountry: any;
  voteMapData: Record<string, string>;
};

// ── Vote-specific constants ────────────────────────────────────────────────────

const VOTE_META: Record<VoteCategory, { label: string; color: string; bg: string; text: string }> = {
  yes:     { label: "In Favour", color: "#22c55e", bg: "bg-green-500/15",  text: "text-green-400"  },
  no:      { label: "Against",   color: "#ef4444", bg: "bg-red-500/15",    text: "text-red-400"    },
  abstain: { label: "Abstain",   color: "#eab308", bg: "bg-yellow-500/15", text: "text-yellow-400" },
  absent:  { label: "Absent",    color: "#9ca3af", bg: "bg-gray-500/15",   text: "text-gray-400"   },
};

const CATEGORIES: VoteCategory[] = ["yes", "no", "abstain", "absent"];

// ── Donut chart ───────────────────────────────────────────────────────────────

function DonutChart({
  summary,
}: {
  summary: Record<string, number>;
}) {
  const total = CATEGORIES.reduce((s, k) => s + (summary[k] ?? 0), 0);
  if (total === 0) return null;

  const cx = 54, cy = 54, r = 40, sw = 14;
  const circumference = 2 * Math.PI * r;

  let cumulativeFraction = 0;
  const segments = CATEGORIES.map((key) => {
    const value    = summary[key] ?? 0;
    const fraction = value / total;
    const offset   = cumulativeFraction;
    cumulativeFraction += fraction;
    return { key, value, fraction, offset };
  }).filter((s) => s.value > 0);

  return (
    <svg
      width={108}
      height={108}
      viewBox="0 0 108 108"
      className="shrink-0"
    >
      {segments.map(({ key, fraction, offset }) => {
        const dashLen   = fraction * circumference;
        const dashOffset = circumference * (0.25 - offset); // start from top

        return (
          <circle
            key={key}
            cx={cx}
            cy={cy}
            r={r}
            fill="none"
            stroke={VOTE_META[key as VoteCategory].color}
            strokeWidth={sw}
            strokeDasharray={`${dashLen} ${circumference - dashLen}`}
            strokeDashoffset={dashOffset}
          />
        );
      })}
      {/* Dark centre hole */}
      <circle cx={cx} cy={cy} r={r - sw / 2 - 2} fill="#030712" />
      {/* Total count label */}
      <text
        x={cx}
        y={cy - 4}
        textAnchor="middle"
        fontSize={14}
        fontWeight={700}
        fill="white"
      >
        {total}
      </text>
      <text
        x={cx}
        y={cy + 11}
        textAnchor="middle"
        fontSize={8}
        fill="#6b7280"
      >
        Total
      </text>
    </svg>
  );
}

// ── Country list item ─────────────────────────────────────────────────────────

function CountryRow({ country, voteKey }: { country: any; voteKey: VoteCategory }) {
  const meta = VOTE_META[voteKey];
  return (
    <div className="flex items-center gap-3 py-2 border-b border-gray-800/60 last:border-0">
      {/* Flag */}
      <div className="h-5 w-7 overflow-hidden rounded shrink-0 border border-gray-700/50">
        <img
          src={`https://flagcdn.com/w40/${(country.isoa2 || "").toLowerCase()}.png`}
          alt={country.name}
          className="h-full w-full object-cover"
          onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
        />
      </div>
      {/* Name */}
      <span className="flex-1 text-xs text-gray-200 truncate">{country.name}</span>
      {/* Vote badge */}
      <span className={`text-[10px] font-medium ${meta.text} shrink-0`}>
        {meta.label}
      </span>
    </div>
  );
}

// ── Empty state ───────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <aside
      className="
        w-full sm:w-[360px] xl:w-[380px] shrink-0
        border-l border-gray-800/80 bg-[#030712]
        flex items-center justify-center p-8
      "
    >
      <div className="text-center max-w-[260px]">
        <div className="mx-auto mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-gray-800 bg-gray-900/60 text-3xl">
          🗳️
        </div>
        <h2 className="text-lg font-semibold text-white">No Resolution Selected</h2>
        <p className="mt-3 text-sm leading-6 text-gray-400">
          Select a resolution from the left panel to view detailed voting
          records and country breakdowns.
        </p>
      </div>
    </aside>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────

export default function UNVotingPanel({
  selectedResolution,
  votesSummary,
  votesByCategory,
  selectedCountry,
  voteMapData,
}: Props) {
  const [activeTab, setActiveTab] = useState<VoteCategory>("yes");

  if (!selectedResolution) return <EmptyState />;

  const total = CATEGORIES.reduce((s, k) => s + (votesSummary[k] ?? 0), 0);
  const selectedVote = selectedCountry
    ? (voteMapData[selectedCountry.isoa3_code] as VoteCategory | undefined)
    : undefined;

  return (
    <aside
      className="
        w-full sm:w-[360px] xl:w-[380px] shrink-0
        border-l border-gray-800/80 bg-[#030712]
        flex flex-col min-h-0
      "
    >
      {/* ── Header ── */}
      <div
        className="
          shrink-0 border-b border-gray-800/80
          bg-gradient-to-b from-[#061120] to-[#030712]
          px-4 pt-4 pb-4
        "
      >
        <h2 className="text-[15px] font-semibold text-white tracking-tight mb-3">
          Resolution Details
        </h2>

        {/* Meta rows */}
        <div className="space-y-1.5 text-xs">
          <div className="flex items-start gap-2">
            <span className="text-gray-500 w-20 shrink-0">Resolution</span>
            <span className="font-mono font-semibold bg-amber-500/20 text-amber-300 px-1.5 py-0.5 rounded text-[11px]">
              {selectedResolution.un_symbol || `#${selectedResolution.id}`}
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="text-gray-500 w-20 shrink-0">Title</span>
            <span className="text-gray-200 leading-snug">{selectedResolution.title}</span>
          </div>
          <div className="flex items-start gap-2">
            <span className="text-gray-500 w-20 shrink-0">Date</span>
            <span className="text-gray-300">
              {selectedResolution.vote_date
                ? new Date(selectedResolution.vote_date).toLocaleDateString("en-US", {
                    year: "numeric", month: "long", day: "numeric",
                  })
                : "—"}
            </span>
          </div>
          <div className="flex items-start gap-2">
            <span className="text-gray-500 w-20 shrink-0">Body</span>
            <span className="text-gray-300">{selectedResolution.body || "UNGA"}</span>
          </div>
        </div>

        {/* Summary / short description */}
        {(selectedResolution.short_description || selectedResolution.explanation) && (
          <div className="mt-3">
            <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider mb-1">
              Summary
            </p>
            <p className="text-xs text-gray-300 leading-relaxed line-clamp-4">
              {selectedResolution.short_description || selectedResolution.explanation}
            </p>
          </div>
        )}
      </div>

      {/* ── Scrollable body ── */}
      <div
        className="
          flex-1 overflow-y-auto px-4 py-4 space-y-5
          [scrollbar-width:none] [&::-webkit-scrollbar]:hidden
        "
      >

        {/* Selected country highlight */}
        {selectedCountry && selectedVote && (
          <div
            className={`
              rounded-xl border px-3 py-2.5
              ${VOTE_META[selectedVote].bg}
              border-${selectedVote === "yes" ? "green" : selectedVote === "no" ? "red" : selectedVote === "abstain" ? "yellow" : "gray"}-500/20
            `}
          >
            <p className="text-[11px] text-gray-500 mb-1">Selected country</p>
            <div className="flex items-center gap-2.5">
              <div className="h-6 w-8 overflow-hidden rounded shrink-0 border border-gray-700/50">
                <img
                  src={`https://flagcdn.com/w40/${(selectedCountry.isoa2_code || "").toLowerCase()}.png`}
                  alt={selectedCountry.country_name}
                  className="h-full w-full object-cover"
                  onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                />
              </div>
              <span className="text-sm font-medium text-white flex-1">
                {selectedCountry.country_name}
              </span>
              <span className={`text-xs font-semibold ${VOTE_META[selectedVote].text}`}>
                {VOTE_META[selectedVote].label}
              </span>
            </div>
          </div>
        )}

        {/* Vote breakdown */}
        {total > 0 && (
          <section>
            <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider mb-3">
              Vote Breakdown
            </p>

            <div className="flex items-center gap-4">
              <DonutChart summary={votesSummary} />

              <div className="flex-1 space-y-2">
                {CATEGORIES.map((key) => {
                  const count = votesSummary[key] ?? 0;
                  if (count === 0) return null;
                  const pct = ((count / total) * 100).toFixed(1);
                  const meta = VOTE_META[key];
                  return (
                    <div key={key} className="flex items-center gap-2">
                      <span
                        className="w-2.5 h-2.5 rounded-sm shrink-0"
                        style={{ backgroundColor: meta.color }}
                      />
                      <span className="text-xs text-gray-300 flex-1">{meta.label}</span>
                      <span className="text-xs font-semibold text-white tabular-nums">
                        {count}
                      </span>
                      <span className="text-[10px] text-gray-500 tabular-nums w-10 text-right">
                        ({pct}%)
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        )}

        {/* Top country votes */}
        {total > 0 && (
          <section>
            <p className="text-[11px] font-medium text-gray-500 uppercase tracking-wider mb-3">
              Top Country Votes
            </p>

            {/* Tabs */}
            <div className="flex gap-1 mb-3 flex-wrap">
              {CATEGORIES.map((key) => {
                const count = votesSummary[key] ?? 0;
                if (count === 0) return null;
                const meta = VOTE_META[key];
                return (
                  <button
                    key={key}
                    onClick={() => setActiveTab(key)}
                    className={`
                      px-2.5 py-1 rounded-lg text-[11px] font-medium transition-colors
                      ${
                        activeTab === key
                          ? `${meta.bg} ${meta.text} ring-1 ring-current/30`
                          : "bg-gray-800/60 text-gray-400 hover:bg-gray-800"
                      }
                    `}
                  >
                    {meta.label}
                    <span className="ml-1 opacity-70">({count})</span>
                  </button>
                );
              })}
            </div>

            {/* Country list */}
            <div className="rounded-xl border border-gray-800 bg-gray-900/40 px-3 max-h-64 overflow-y-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
              {(votesByCategory[activeTab] ?? []).length === 0 ? (
                <p className="py-4 text-center text-xs text-gray-500">
                  No countries voted "{VOTE_META[activeTab].label}"
                </p>
              ) : (
                (votesByCategory[activeTab] ?? []).map((country: any) => (
                  <CountryRow
                    key={country.isoa3}
                    country={country}
                    voteKey={activeTab}
                  />
                ))
              )}
            </div>
          </section>
        )}

        {/* No vote data yet */}
        {total === 0 && (
          <div className="flex flex-col items-center justify-center py-10 text-center gap-3">
            <div className="text-3xl">📊</div>
            <p className="text-sm font-medium text-white">Vote data loading…</p>
            <p className="text-xs text-gray-400 max-w-[220px]">
              Individual country vote records may not be available for all resolutions.
            </p>
          </div>
        )}
      </div>
    </aside>
  );
}
