import { colors } from "../../map/utils/colorScale";
import type { ViewMode } from "../../App";

type LegendItem = { color: string; label: string };

// ── Statements-mode legend ────────────────────────────────────────────────────
const STATEMENT_ITEMS: LegendItem[] = [
  { color: colors[0], label: "No Statements"  },
  { color: colors[1], label: "1–3 Statements" },
  { color: colors[2], label: "4–5 Statements" },
  { color: colors[3], label: "6+ Statements"  },
];

// ── UN-voting-mode legend ─────────────────────────────────────────────────────
const VOTE_ITEMS: LegendItem[] = [
  { color: "#22c55e", label: "In Favour"      },
  { color: "#ef4444", label: "Against"        },
  { color: "#eab308", label: "Abstain"        },
  { color: "#9ca3af", label: "Absent / No Vote" },
  { color: "#1f2937", label: "No Data"        },
];

type Props = { viewMode: ViewMode };

export default function MapLegend({ viewMode }: Props) {
  const items = viewMode === "un_voting" ? VOTE_ITEMS : STATEMENT_ITEMS;

  return (
    <div
      className="
        absolute z-10
        bottom-3 right-3
        md:bottom-5 md:right-5
        lg:bottom-6 lg:right-6
        max-w-[170px] sm:max-w-[190px]
        bg-gray-900/85 backdrop-blur-md
        border border-gray-800
        rounded-lg shadow-lg
        p-2 sm:p-3
        text-[10px] sm:text-xs
        space-y-1.5 sm:space-y-2
      "
    >
      {items.map((item) => (
        <LegendRow key={item.label} {...item} />
      ))}
    </div>
  );
}

function LegendRow({ color, label }: LegendItem) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="w-2.5 h-2.5 sm:w-3 sm:h-3 rounded-sm shrink-0"
        style={{ backgroundColor: color }}
      />
      <span className="text-gray-300 leading-tight">{label}</span>
    </div>
  );
}
