// Glue code (NOT part of the official React Bits component) that mounts
// the real CardSwap into the existing Flask/Jinja dashboard.
//
// Data flow: src/dashboard/app.py's existing _manual_application_cards()
// (unchanged logic, just two extra fields) serializes the real
// READY_TO_APPLY jobs into a <script type="application/json"> island in
// dashboard.html. This file reads that JSON — there is no second job
// retrieval system, no fetch() call, no API added.
//
// CardSwap's own slot math grows with the number of cards (each extra
// card adds cardDistance px of horizontal spread and verticalDistance px
// of vertical spread) and every swap includes a hard-coded 500px drop —
// it's built as a hero-section widget. To keep the official, unmodified
// animation looking right inside a dashboard column instead of an
// oversized or overflowing mess, this shows the 3 highest fit-score jobs
// (matching the official demo's 3-card example) in the swap stack; the
// full READY_TO_APPLY list still renders below it exactly as it already
// did, unchanged, so nothing is hidden — see dashboard.html.
import React from "react";
import { createRoot } from "react-dom/client";
import CardSwap, { Card } from "./CardSwap.jsx";
// Imported as raw text (not a side-effecting stylesheet import) so it can
// be injected into a shadow root below, instead of the document <head>.
// See the shadow-DOM note in mount() for why.
import cardSwapCssText from "./CardSwap.css?inline";
import cardContentCssText from "./card-content.css?inline";

const MAX_CARDS = 3;

function readJobs() {
  const el = document.getElementById("cardswap-jobs-data");
  if (!el) return [];
  try {
    const jobs = JSON.parse(el.textContent);
    return Array.isArray(jobs) ? jobs : [];
  } catch {
    return [];
  }
}

function openJob(href) {
  if (href) window.open(href, "_blank", "noopener,noreferrer");
}

// Same 75 / 50 thresholds the Jinja job cards use (see dashboard.html's
// score--high / score--mid / score--low logic), so a given score is
// coloured identically whether it appears in the swap stack or the list.
function scoreTier(score) {
  if (score >= 75) return "high";
  if (score >= 50) return "mid";
  return "low";
}

// A plain function that RETURNS a <Card> element directly — not a
// separate <JobCard/> component wrapping <Card>. This matters: CardSwap
// attaches its GSAP ref via `cloneElement(child, { ref, style, ... })`
// on whatever element is passed as its direct child. A custom function
// COMPONENT in between (e.g. <JobCard job={job} />) would silently
// swallow that injected ref — plain function components don't forward
// refs — leaving GSAP animating nothing. Returning <Card> itself here
// keeps CardSwap's ref landing exactly where the official usage pattern
// (`<CardSwap><Card>...</Card></CardSwap>`) expects it to.
function renderJobCard(job) {
  return (
    <Card
      key={job.job_id}
      className="job-card"
      role="link"
      tabIndex={0}
      onClick={() => openJob(job.href)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openJob(job.href);
        }
      }}
    >
      <div className="job-card-top">
        <img className="job-card-img" src={job.image} alt="" />
        {typeof job.fit_score === "number" && (
          <span className="job-card-score" data-tier={scoreTier(job.fit_score)}>
            {job.fit_score}
          </span>
        )}
      </div>
      <div className="job-card-title">{job.title}</div>
      <div className="job-card-company">{job.company || "Unknown company"}</div>
      {job.location && <div className="job-card-location">{job.location}</div>}
      {job.reason && <div className="job-card-reason">{job.reason}</div>}
      <div className="job-card-cta">Apply &rarr;</div>
    </Card>
  );
}

function mount() {
  const root = document.getElementById("cardswap-root");
  if (!root) return;

  const jobs = readJobs().slice(0, MAX_CARDS);
  if (jobs.length === 0) {
    root.innerHTML = '<p class="empty">No jobs</p>';
    return;
  }

  // Mount inside a shadow root rather than directly into #cardswap-root.
  // The official CardSwap.css defines a class literally named ".card" —
  // and this dashboard's OWN pre-existing style.css already uses ".card"
  // for every job card on the page (see dashboard.html's plain job list).
  // Loading both as regular page <link>/<style> tags means whichever
  // loads second wins the naming collision and silently breaks the
  // other's cards. Shadow DOM fully encapsulates CardSwap's CSS both
  // ways — nothing it defines can leak out, and nothing the dashboard
  // defines leaks in — without renaming any class in either stylesheet.
  const shadow = root.attachShadow({ mode: "open" });
  const style = document.createElement("style");
  style.textContent = `${cardSwapCssText}\n${cardContentCssText}`;
  shadow.appendChild(style);
  const mountPoint = document.createElement("div");
  shadow.appendChild(mountPoint);

  createRoot(mountPoint).render(
    <CardSwap
      width={260}
      height={170}
      cardDistance={35}
      verticalDistance={42}
      delay={5000}
      pauseOnHover={false}
      skewAmount={6}
      easing="elastic"
    >
      {jobs.map(renderJobCard)}
    </CardSwap>
  );
}

mount();
