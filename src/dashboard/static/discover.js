// Landing-page "Find new jobs" button — progressive enhancement.
//
// Without this script the form is a plain POST to /discover: it runs
// discovery synchronously, then flashes a summary and redirects. The
// script keeps that fallback intact and only adds a nicer UX on top:
// it intercepts the submit, hits /api/discover instead (JSON response,
// no redirect), and updates the button + a small live-region line
// with a friendly summary — no HTTP status codes, no crawler jargon.
(function () {
  var form = document.getElementById("discover-form");
  var btn = document.getElementById("discover-btn");
  var statusEl = document.getElementById("discover-status");
  if (!form || !btn || !statusEl) return;

  function setStatus(text, tone) {
    statusEl.hidden = false;
    statusEl.textContent = text;
    statusEl.className = "hero__status" + (tone ? " hero__status--" + tone : "");
  }

  function humanSummary(result) {
    var inserted = Number(result.inserted || 0);
    var duplicates = Number(result.duplicates || 0);
    var pipeline = result.pipeline || {};
    var rejected = Number(pipeline.rejected || 0);
    var readyToApply = Number(pipeline.ready_to_apply || 0);

    if (inserted === 0) {
      // Nothing new. Say so plainly.
      return duplicates > 0
        ? "You're up to date. Nothing new since the last check."
        : "No jobs found this time.";
    }

    var parts = [inserted + " new " + (inserted === 1 ? "job" : "jobs") + " found"];
    if (readyToApply > 0) {
      parts.push(readyToApply + " ready to review");
    }
    return parts.join(" · ") + ".";
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    btn.disabled = true;
    var originalLabel = btn.textContent;
    btn.textContent = "Finding new jobs…";
    setStatus("Checking every active source. This can take a minute or two.", "working");

    fetch("/api/discover", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ process: true }),
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { ok: r.ok, data: data };
        });
      })
      .then(function (out) {
        if (!out.ok || !out.data || out.data.ok === false) {
          // Show a calm message; keep the crawler-shaped detail out.
          setStatus("Some sources need attention. Try again shortly.", "warn");
        } else {
          setStatus(humanSummary(out.data), "ok");
          // A soft reload picks up the new rows for the overview tiles and
          // the recent-jobs list without any client-side re-render code.
          setTimeout(function () {
            window.location.reload();
          }, 1200);
        }
      })
      .catch(function () {
        setStatus("Couldn't finish that check. Try again shortly.", "warn");
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = originalLabel;
      });
  });
})();
