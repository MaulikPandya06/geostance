import { useEffect, useState } from "react";

import AppLayout from "./components/layout/AppLayout";
import EventsPanel from "./features/events/EventsPanel";
import MapSection from "./features/events/MapSection";
import CountryPanel from "./features/events/CountryPanel";
import UNVotingPanel from "./features/events/UNVotingPanel";
import type { HeatmapCountry } from "./types/heatmap";

export type ViewMode = "statements" | "un_voting";

export default function App() {
  // ── Shared state ────────────────────────────────────────────────────────
  const [viewMode, setViewMode] = useState<ViewMode>("statements");
  const [selectedEvent, setSelectedEvent] = useState<any>(null);
  const [selectedCountry, setSelectedCountry] = useState<any>(null);

  // ── Statements mode ─────────────────────────────────────────────────────
  const [heatmapData, setHeatmapData] = useState<HeatmapCountry[]>([]);
  const [countryStatements, setCountryStatements] = useState<any[]>([]);

  // ── UN Voting mode ──────────────────────────────────────────────────────
  const [selectedResolution, setSelectedResolution] = useState<any>(null);
  const [voteMapData, setVoteMapData] = useState<Record<string, string>>({});
  const [votesSummary, setVotesSummary] = useState<Record<string, number>>({});
  const [votesByCategory, setVotesByCategory] = useState<Record<string, any[]>>({});

  // Reset country-level state when event changes
  useEffect(() => {
    setSelectedCountry(null);
    setCountryStatements([]);
  }, [selectedEvent]);

  // Reset resolution + country state when switching modes
  useEffect(() => {
    setSelectedResolution(null);
    setVoteMapData({});
    setVotesSummary({});
    setVotesByCategory({});
    setSelectedCountry(null);
    setCountryStatements([]);
  }, [viewMode]);

  return (
    <AppLayout>
      <EventsPanel
        viewMode={viewMode}
        setViewMode={setViewMode}
        selectedEvent={selectedEvent}
        setSelectedEvent={setSelectedEvent}
        setHeatmapData={setHeatmapData}
        selectedResolution={selectedResolution}
        setSelectedResolution={setSelectedResolution}
        setVoteMapData={setVoteMapData}
        setVotesSummary={setVotesSummary}
        setVotesByCategory={setVotesByCategory}
      />

      <MapSection
        viewMode={viewMode}
        heatmapData={heatmapData}
        selectedEvent={selectedEvent}
        setSelectedCountry={setSelectedCountry}
        setCountryStatements={setCountryStatements}
        voteMapData={voteMapData}
        selectedResolution={selectedResolution}
        votesSummary={votesSummary}
      />

      {viewMode === "statements" ? (
        <CountryPanel
          selectedCountry={selectedCountry}
          countryStatements={countryStatements}
          selectedEvent={selectedEvent}
        />
      ) : (
        <UNVotingPanel
          selectedResolution={selectedResolution}
          votesSummary={votesSummary}
          votesByCategory={votesByCategory}
          selectedCountry={selectedCountry}
          voteMapData={voteMapData}
        />
      )}
    </AppLayout>
  );
}
