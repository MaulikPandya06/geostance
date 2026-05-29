import { useEffect, useState } from "react";

import AppLayout from "./components/layout/AppLayout";
import { useGlobalStore } from "./store/useGlobalStore";
import EventsPanel from "./features/events/EventsPanel";
import MapSection from "./features/events/MapSection";
import CountryPanel from "./features/events/CountryPanel";
import UNVotingPanel from "./features/events/UNVotingPanel";
import AskAIPanel from "./features/events/AskAIPanel";
import type { HeatmapCountry } from "./types/heatmap";

export type ViewMode = "statements" | "un_voting";

export type ResolutionType = {
  id: number;
  un_symbol: string;
  title: string;
  vote_date: string;
  body: string;
};

export default function App() {
  // ── Shared state ────────────────────────────────────────────────────────
  const [viewMode, setViewMode] = useState<ViewMode>("un_voting");
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

  // ── Multi-resolution selection & Ask AI ─────────────────────────────────
  const [selectedResolutions, setSelectedResolutions] = useState<ResolutionType[]>([]);
  const [showAskAI, setShowAskAI] = useState(false);

  // Reset country-level state when event changes
  useEffect(() => {
    setSelectedCountry(null);
    setCountryStatements([]);
  }, [selectedEvent]);

  // Reset all resolution state when switching modes
  useEffect(() => {
    setSelectedResolution(null);
    setVoteMapData({});
    setVotesSummary({});
    setVotesByCategory({});
    setSelectedCountry(null);
    setCountryStatements([]);
    setSelectedResolutions([]);
    setShowAskAI(false);
  }, [viewMode]);

  const handleClearSelection = () => setSelectedResolutions([]);

  const handleDownload = async () => {
    if (selectedResolutions.length === 0) return;
    const API_URL = import.meta.env.VITE_API_URL;
    useGlobalStore.setState({ isLoading: true });
    try {
      const resp = await fetch(`${API_URL}/api/un-resolutions/generate-report/`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          resolution_ids: selectedResolutions.map((r) => r.id),
          feature: "all",
        }),
      });
      if (!resp.ok) return;
      const blob        = await resp.blob();
      const url         = URL.createObjectURL(blob);
      const a           = document.createElement("a");
      const disposition = resp.headers.get("Content-Disposition") ?? "";
      const nameMatch   = disposition.match(/filename="?([^"]+)"?/);
      a.download        = nameMatch?.[1] ?? "GeoStance_report.docx";
      a.href            = url;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Download failed", err);
    } finally {
      useGlobalStore.setState({ isLoading: false });
    }
  };

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
        selectedResolutions={selectedResolutions}
        setSelectedResolutions={setSelectedResolutions}
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
        selectedResolutions={selectedResolutions}
        onClearSelection={handleClearSelection}
        onDownload={handleDownload}
        onAskAI={() => setShowAskAI(true)}
      />

      {viewMode === "statements" ? (
        <CountryPanel
          selectedCountry={selectedCountry}
          countryStatements={countryStatements}
          selectedEvent={selectedEvent}
        />
      ) : (
        <>
          <UNVotingPanel
            selectedResolution={selectedResolution}
            votesSummary={votesSummary}
            votesByCategory={votesByCategory}
            selectedCountry={selectedCountry}
            voteMapData={voteMapData}
          />
          {showAskAI && (
            <AskAIPanel
              selectedResolutions={selectedResolutions}
              onClose={() => setShowAskAI(false)}
            />
          )}
        </>
      )}
    </AppLayout>
  );
}
