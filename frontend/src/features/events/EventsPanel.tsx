import { useEffect, useRef, useState } from "react";
import type { HeatmapCountry } from "../../types/heatmap";
import { fetchWithLoading } from "../../services/fetchWithLoading";
import type { ViewMode, ResolutionType } from "../../App";

type EventType = {
  id: number;
  title: string;
  description: string;
  start_date: string;
  end_date: string | null;
};

type Props = {
  viewMode: ViewMode;
  setViewMode: (mode: ViewMode) => void;
  selectedEvent: any;
  setSelectedEvent: (event: any) => void;
  setHeatmapData: React.Dispatch<React.SetStateAction<HeatmapCountry[]>>;
  selectedResolution: any;
  setSelectedResolution: (resolution: any) => void;
  setVoteMapData: (data: Record<string, string>) => void;
  setVotesSummary: (summary: Record<string, number>) => void;
  setVotesByCategory: (byCategory: Record<string, any[]>) => void;
  selectedResolutions: ResolutionType[];
  setSelectedResolutions: (rs: ResolutionType[]) => void;
};

export default function EventsPanel({
  viewMode,
  setViewMode,
  selectedEvent,
  setSelectedEvent,
  setHeatmapData,
  selectedResolution,
  setSelectedResolution,
  setVoteMapData,
  setVotesSummary,
  setVotesByCategory,
  selectedResolutions,
  setSelectedResolutions,
}: Props) {
  const [events, setEvents] = useState<EventType[]>([]);
  const [expandedEventId, setExpandedEventId] = useState<number | null>(null);
  const [eventResolutions, setEventResolutions] =
    useState<Record<number, ResolutionType[]>>({});
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const API_URL = import.meta.env.VITE_API_URL;

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Initial event fetch
  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetchWithLoading(`${API_URL}/api/events/`);
        const data: EventType[] = await res.json();
        setEvents(data);
        if (data.length > 0) handleEventClick(data[0], viewMode);
      } catch (err) {
        console.error(err);
      }
    };
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // When switching to UN voting mode, expand + fetch the already-selected event
  useEffect(() => {
    if (viewMode === "un_voting" && selectedEvent) {
      setExpandedEventId(selectedEvent.id);
      fetchResolutionsForEvent(selectedEvent.id);
    }
  }, [viewMode]);

  // ── helpers ───────────────────────────────────────────────────────────────

  const fetchResolutionsForEvent = async (eventId: number) => {
    if (eventResolutions[eventId]) return; // already cached
    try {
      const res = await fetch(`${API_URL}/api/events/${eventId}/resolutions/`);
      const data: ResolutionType[] = await res.json();
      setEventResolutions((prev) => ({ ...prev, [eventId]: data }));
    } catch (err) {
      console.error(err);
    }
  };

  const handleEventClick = async (event: EventType, mode: ViewMode = viewMode) => {
    setSelectedEvent(event);

    if (mode === "statements") {
      try {
        const res = await fetchWithLoading(
          `${API_URL}/api/events/${event.id}/heatmap/`
        );
        setHeatmapData(await res.json());
      } catch (err) {
        console.error(err);
      }
    } else {
      // UN voting mode: expand the event and load its resolutions
      const isAlreadyExpanded = expandedEventId === event.id;
      setExpandedEventId(isAlreadyExpanded ? null : event.id);
      if (!isAlreadyExpanded) fetchResolutionsForEvent(event.id);
    }
  };

  const handleResolutionClick = async (res: ResolutionType) => {
    // Optimistic: show symbol immediately
    setSelectedResolution(res);
    setVoteMapData({});
    setVotesSummary({});
    setVotesByCategory({});

    try {
      const response = await fetchWithLoading(
        `${API_URL}/api/un-resolutions/${res.id}/vote-map/`
      );
      const data = await response.json();
      setSelectedResolution(data.resolution);
      setVoteMapData(data.vote_map);
      setVotesSummary(data.votes_summary);
      setVotesByCategory(data.by_category);
    } catch (err) {
      console.error(err);
    }
  };

  const handleModeSwitch = (mode: ViewMode) => {
    setViewMode(mode);
    setDropdownOpen(false);

    if (mode === "statements" && selectedEvent) {
      fetch(`${API_URL}/api/events/${selectedEvent.id}/heatmap/`)
        .then((r) => r.json())
        .then(setHeatmapData)
        .catch(console.error);
    }
    if (mode === "un_voting" && selectedEvent) {
      setExpandedEventId(selectedEvent.id);
      fetchResolutionsForEvent(selectedEvent.id);
    }
  };

  // ── render ────────────────────────────────────────────────────────────────

  return (
    <aside
      className="
        w-full sm:w-[320px] lg:w-[340px]
        shrink-0
        border-r border-gray-800
        bg-gray-950
        flex flex-col
        min-h-0
      "
    >
      {/* ── Header ── */}
      <div className="px-5 py-4 border-b border-gray-800 shrink-0 space-y-3">
        <h2 className="text-lg font-semibold text-white tracking-tight">
          Global Events
        </h2>

        {/* Mode dropdown */}
        <div className="relative" ref={dropdownRef}>
          <button
            onClick={() => setDropdownOpen((o) => !o)}
            className="
              w-full flex items-center justify-between gap-2
              rounded-xl border border-gray-700 bg-gray-900
              px-3 py-2 text-sm text-gray-200
              hover:border-gray-600 transition-colors
            "
          >
            <div className="flex items-center gap-2">
              <span className="text-sm">
                {viewMode === "un_voting" ? "🗳️" : "💬"}
              </span>
              <span>{viewMode === "un_voting" ? "UN Voting" : "Statements"}</span>
            </div>
            <svg
              className={`w-4 h-4 text-gray-400 transition-transform ${
                dropdownOpen ? "rotate-180" : ""
              }`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M19 9l-7 7-7-7"
              />
            </svg>
          </button>

          {dropdownOpen && (
            <div
              className="
                absolute top-full left-0 right-0 mt-1 z-50
                rounded-xl border border-gray-700 bg-gray-900
                shadow-2xl overflow-hidden
              "
            >
              {(
                [
                  { value: "statements" as ViewMode, label: "Statements", icon: "💬" },
                  { value: "un_voting" as ViewMode, label: "UN Voting", icon: "🗳️" },
                ] as const
              ).map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => handleModeSwitch(opt.value)}
                  className={`
                    w-full flex items-center gap-2.5 px-3 py-2.5 text-sm transition-colors
                    ${
                      viewMode === opt.value
                        ? "bg-blue-500/15 text-blue-300"
                        : "text-gray-300 hover:bg-gray-800"
                    }
                  `}
                >
                  <span>{opt.icon}</span>
                  <span>{opt.label}</span>
                  {viewMode === opt.value && (
                    <svg
                      className="ml-auto w-3.5 h-3.5 text-blue-400"
                      fill="currentColor"
                      viewBox="0 0 20 20"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Events List ── */}
      <div
        className="
          flex-1 overflow-y-auto p-4 space-y-3
          [scrollbar-width:none] [&::-webkit-scrollbar]:hidden
        "
      >
        {events.map((event) => {
          const isSelected = selectedEvent?.id === event.id;
          const isExpanded = viewMode === "un_voting" && expandedEventId === event.id;
          const resolutions = eventResolutions[event.id] ?? [];

          return (
            <div key={event.id}>
              {/* Event card */}
              <button
                onClick={() => handleEventClick(event)}
                className={`
                  group w-full rounded-2xl border p-4 text-left
                  transition-all duration-200 backdrop-blur-sm
                  hover:-translate-y-[1px]
                  ${
                    isSelected
                      ? "border-blue-500/40 bg-blue-500/10 shadow-lg shadow-blue-500/10"
                      : "border-gray-800 bg-gray-900/70 hover:border-gray-700 hover:bg-gray-900"
                  }
                `}
              >
                <div className="flex items-start justify-between gap-3">
                  <h3
                    className={`
                      text-sm sm:text-[15px] font-semibold leading-snug transition-colors
                      ${isSelected ? "text-blue-100" : "text-gray-100 group-hover:text-white"}
                    `}
                  >
                    {event.title}
                  </h3>
                  <span
                    className={`
                      shrink-0 rounded-full px-2 py-1
                      text-[10px] font-medium uppercase tracking-wide
                      ${
                        event.end_date
                          ? "bg-gray-800 text-gray-300"
                          : "bg-emerald-500/15 text-emerald-400 border border-emerald-500/20"
                      }
                    `}
                  >
                    {event.end_date ? "Ended" : "Ongoing"}
                  </span>
                </div>

                <div className="mt-3 flex items-center gap-2 text-xs sm:text-sm text-gray-400">
                  <span>{new Date(event.start_date).toLocaleDateString()}</span>
                  <span className="text-gray-600">•</span>
                  <span>
                    {event.end_date
                      ? new Date(event.end_date).toLocaleDateString()
                      : "Present"}
                  </span>
                </div>

                {/* Resolution count row — UN voting mode only */}
                {viewMode === "un_voting" && (
                  <div className="mt-2 flex items-center justify-between">
                    <span className="text-xs text-gray-500">
                      {eventResolutions[event.id]
                        ? `${resolutions.length} Resolution${resolutions.length !== 1 ? "s" : ""}`
                        : isSelected
                        ? "Loading…"
                        : "Click to expand"}
                    </span>
                    <svg
                      className={`w-3.5 h-3.5 text-gray-500 transition-transform duration-200 ${
                        isExpanded ? "rotate-180" : ""
                      }`}
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={2}
                        d="M19 9l-7 7-7-7"
                      />
                    </svg>
                  </div>
                )}
              </button>

              {/* Resolution list — expanded in UN voting mode */}
              {viewMode === "un_voting" && isExpanded && resolutions.length > 0 && (
                <div className="mt-1 ml-3 border-l border-gray-800 pl-3 space-y-1">
                  {resolutions.map((res) => {
                    const isResSelected = selectedResolution?.id === res.id;
                    const isChecked     = selectedResolutions.some((r) => r.id === res.id);

                    const toggleCheck = (e: React.MouseEvent) => {
                      e.stopPropagation();
                      if (isChecked) {
                        setSelectedResolutions(selectedResolutions.filter((r) => r.id !== res.id));
                      } else {
                        setSelectedResolutions([...selectedResolutions, res]);
                      }
                    };

                    return (
                      <div key={res.id} className="flex items-start gap-2">
                        {/* Checkbox */}
                        <button
                          onClick={toggleCheck}
                          className="mt-2.5 shrink-0 flex items-center justify-center"
                          aria-label={isChecked ? "Deselect resolution" : "Select resolution"}
                        >
                          <div
                            className={`
                              w-4 h-4 rounded border-[1.5px] flex items-center justify-center transition-all
                              ${isChecked
                                ? "bg-blue-500 border-blue-500"
                                : "border-gray-600 hover:border-gray-400 bg-transparent"}
                            `}
                          >
                            {isChecked && (
                              <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 12 12" stroke="currentColor" strokeWidth={2.5}>
                                <path strokeLinecap="round" strokeLinejoin="round" d="M2 6l3 3 5-5" />
                              </svg>
                            )}
                          </div>
                        </button>

                        {/* Resolution row */}
                        <button
                          onClick={() => handleResolutionClick(res)}
                          className={`
                            flex-1 rounded-xl border px-3 py-2.5 text-left
                            transition-all duration-150
                            ${
                              isResSelected
                                ? "border-blue-500/40 bg-blue-500/10"
                                : isChecked
                                ? "border-blue-500/20 bg-blue-500/5"
                                : "border-gray-800/60 bg-gray-900/40 hover:border-gray-700 hover:bg-gray-900/70"
                            }
                          `}
                        >
                          <span
                            className={`
                              inline-block text-[10px] font-mono font-semibold
                              px-1.5 py-0.5 rounded
                              ${
                                isResSelected
                                  ? "bg-amber-500/20 text-amber-300"
                                  : "bg-gray-800 text-gray-400"
                              }
                            `}
                          >
                            {res.un_symbol || `#${res.id}`}
                          </span>

                          <p
                            className={`mt-1 text-xs leading-snug ${
                              isResSelected ? "text-blue-100" : "text-gray-300"
                            }`}
                          >
                            {res.title.length > 52
                              ? res.title.slice(0, 52) + "…"
                              : res.title}
                          </p>

                          <p className="mt-0.5 text-[10px] text-gray-500">
                            {res.vote_date
                              ? new Date(res.vote_date).toLocaleDateString()
                              : ""}
                          </p>
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Footer ── */}
      <div className="shrink-0 border-t border-gray-800 p-4">
        <div
          className="
            flex items-start gap-3
            rounded-2xl border border-blue-500/20 bg-blue-500/10
            px-4 py-3 backdrop-blur-sm
          "
        >
          <div
            className="
              mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center
              rounded-full bg-blue-500/20 text-[11px] font-bold text-blue-300
            "
          >
            i
          </div>
          <div>
            <p className="text-xs font-medium tracking-wide text-blue-200">Info</p>
            <p className="mt-1 text-xs leading-relaxed text-blue-100/80">
              {viewMode === "un_voting"
                ? "UN voting data sourced from official UN documents."
                : "More global events and geopolitical developments will be added soon."}
            </p>
          </div>
        </div>
      </div>
    </aside>
  );
}
