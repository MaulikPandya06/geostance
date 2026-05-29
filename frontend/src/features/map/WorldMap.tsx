import { useEffect, useRef } from "react";
import * as d3 from "d3";
import { feature } from "topojson-client";
import worldData from "./world-110m.json";
import colorScale from "../../map/utils/colorScale";
import { fetchWithLoading } from "../../services/fetchWithLoading";
import type { ViewMode } from "../../App";

// Vote → fill colour mapping (matches legend)
const VOTE_COLOR: Record<string, string> = {
  yes:     "#22c55e",   // green-500
  no:      "#ef4444",   // red-500
  abstain: "#eab308",   // yellow-500
  absent:  "#9ca3af",   // gray-400
};
const NO_DATA_COLOR = "#1f2937";   // gray-800

const VOTE_LABEL: Record<string, string> = {
  yes:     "In Favour",
  no:      "Against",
  abstain: "Abstain",
  absent:  "Absent / No Vote",
};

type Props = {
  viewMode: ViewMode;
  heatmapData: any[];
  selectedEvent: any;
  setSelectedCountry: (country: any) => void;
  setCountryStatements: (statements: any[]) => void;
  voteMapData: Record<string, string>;
};

export default function WorldMap({
  viewMode,
  heatmapData,
  selectedEvent,
  setSelectedCountry,
  setCountryStatements,
  voteMapData,
}: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    if (!svgRef.current) return;

    const svg    = d3.select(svgRef.current);
    const width  = svgRef.current.clientWidth;
    const height = svgRef.current.clientHeight;

    svg.selectAll("*").remove();

    // Geo data
    const objectKey =
      (worldData as any).objects.countries ? "countries" : "topo";
    const geo = feature(
      worldData as any,
      (worldData as any).objects[objectKey]
    );

    // Projection
    const projection = d3.geoNaturalEarth1().fitExtent(
      [[16, 16], [width - 16, height - 16]],
      geo as any
    );
    const path = d3.geoPath(projection);

    // Main group (for zoom)
    const g = svg.append("g");

    // Tooltip
    const tooltip = d3
      .select("body")
      .append("div")
      .style("position", "absolute")
      .style("background", "#111827")
      .style("padding", "6px 10px")
      .style("border-radius", "6px")
      .style("font-size", "12px")
      .style("color", "#fff")
      .style("pointer-events", "none")
      .style("opacity", 0);

    // ── Country paths ─────────────────────────────────────────────────────

    g.selectAll("path")
      .data((geo as any).features)
      .join("path")
      .attr("d", path as any)

      // Fill: UN voting mode uses vote colour; statements mode uses heatmap
      .attr("fill", (d: any) => {
        const iso3 = d.properties?.ISO_A3;
        if (viewMode === "un_voting") {
          const vote = voteMapData[iso3];
          return vote ? VOTE_COLOR[vote] ?? NO_DATA_COLOR : NO_DATA_COLOR;
        }
        const country = heatmapData.find((c) => c.isoa3_code === iso3);
        return colorScale(country?.statement_count ?? 0);
      })

      .attr("stroke", "#111827")
      .attr("stroke-width", 0.5)
      .style("cursor", "pointer")

      // Click handler
      .on("click", async function (_, d: any) {
        const iso3        = d.properties?.ISO_A3;
        const countryName = d.properties?.name || d.properties?.NAME || "Unknown";
        const iso2        = (d.properties?.ISO_A2 || "").toLowerCase();

        if (viewMode === "un_voting") {
          // In voting mode just set the selected country — no fetch needed
          setSelectedCountry({
            isoa3_code:   iso3,
            country_name: countryName,
            isoa2_code:   iso2,
          });
          return;
        }

        // Statements mode: fetch country statements from API
        if (!iso3 || !selectedEvent) return;
        const API_URL = import.meta.env.VITE_API_URL;
        try {
          const res = await fetchWithLoading(
            `${API_URL}/api/events/${selectedEvent.id}/countries/${iso3}/statements/`
          );
          const data = await res.json();
          setCountryStatements(data.statements);
          if (data.country) setSelectedCountry(data.country);
        } catch (err) {
          console.error(err);
        }
      })

      // Hover
      .on("mouseover", function (_, d: any) {
        d3.select(this).attr("stroke", "#fff").attr("stroke-width", 1.5);

        const iso3        = d.properties?.ISO_A3;
        const countryName = d.properties?.name || d.properties?.NAME || "Unknown";

        let detail = "";
        if (viewMode === "un_voting") {
          const vote = voteMapData[iso3];
          const label = vote ? VOTE_LABEL[vote] ?? vote : "No Data";
          const color = vote ? VOTE_COLOR[vote] ?? "#6b7280" : "#6b7280";
          detail = `<div style="font-size:11px;color:${color};margin-top:2px;">${label}</div>`;
        } else {
          const country = heatmapData.find((c) => c.isoa3_code === iso3);
          const count   = country?.statement_count ?? 0;
          detail = `<div style="font-size:11px;color:#9ca3af;margin-top:2px;">${count} statement${count !== 1 ? "s" : ""}</div>`;
        }

        tooltip
          .style("opacity", 1)
          .html(`<div style="font-weight:600">${countryName}</div>${detail}`);
      })

      .on("mousemove", function (event) {
        tooltip
          .style("left", event.pageX + 10 + "px")
          .style("top",  event.pageY - 20 + "px");
      })

      .on("mouseout", function () {
        d3.select(this).attr("stroke", "#111827").attr("stroke-width", 0.5);
        tooltip.style("opacity", 0);
      });

    // ── Zoom ─────────────────────────────────────────────────────────────

    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([1, 8])
      .on("zoom", (event) => g.attr("transform", event.transform));

    svg.call(zoom as any);

    const zoomIn  = () => svg.transition().call(zoom.scaleBy as any, 1.3);
    const zoomOut = () => svg.transition().call(zoom.scaleBy as any, 0.7);
    const reset   = () =>
      svg.transition().duration(500).call(zoom.transform as any, d3.zoomIdentity);

    document.getElementById("zoom-in")?.addEventListener("click", zoomIn);
    document.getElementById("zoom-out")?.addEventListener("click", zoomOut);
    document.getElementById("reset")?.addEventListener("click", reset);

    return () => { tooltip.remove(); };

  // Re-render whenever coloring data or mode changes
  }, [heatmapData, voteMapData, viewMode]);

  return (
    <div className="w-full h-full relative">
      {/* Zoom controls */}
      <div className="absolute top-4 left-4 z-10 flex flex-col gap-2">
        <button id="zoom-in"  className="bg-gray-800 px-3 py-2 rounded hover:bg-gray-700 text-white">+</button>
        <button id="zoom-out" className="bg-gray-800 px-3 py-2 rounded hover:bg-gray-700 text-white">−</button>
        <button id="reset"    className="bg-gray-800 px-3 py-2 rounded hover:bg-gray-700 text-white text-xs">Reset</button>
      </div>

      <svg
        ref={svgRef}
        className="w-full h-full block cursor-grab"
        preserveAspectRatio="xMidYMid meet"
      />
    </div>
  );
}
