/* Agora knowledge-graph viz (vanilla JS, no framework, no build step; ADR-0019 §7).
 *
 * Drives the vendored force-graph 1.51.4 (global class `ForceGraph`, MIT, vasturiano) over the
 * first-class JSON `/api/graph` contract. Both the global `/graph` page and the per-note
 * "Connections" embed initialize from THIS one file via the `[data-graph-src]` hook — the only
 * difference is the src URL each container carries.
 *
 * XSS: force-graph's `.nodeLabel(...)` tooltip is injected as innerHTML, so a note title like
 * `<img onerror=...>` would execute if passed raw. `escapeHtml` neutralizes it client-side (the
 * JSON layer stays faithful — it returns the RAW title; escaping is this render layer's job).
 */
"use strict";

/* Escape the five HTML-significant characters so an untrusted title can't break out of the tooltip
 * innerHTML (the force-graph label is set as HTML). The single-quote is included so the result is
 * also safe inside a single-quoted attribute, not only element text — defense-in-depth for reuse. */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/* Build the /note/<id> href. The id is a rel_path that may contain spaces (and other unsafe URL
 * chars), so encode each path SEGMENT but keep the slashes literal for the FastAPI :path route. */
function noteHref(id) {
  return "/note/" + String(id).split("/").map(encodeURIComponent).join("/");
}

/* A deterministic node color: contested status -> warm red; orphan -> muted grey; otherwise a
 * stable hue derived from hashing the node's FIRST subject (or "index" when it declares none)
 * into HSL. Same input -> same color across reloads (no Math.random).
 *
 * A note may carry 0..n subjects (ADR-0041 D2.2) and a canvas node has exactly one fill, so the
 * first entry is a presentation choice, not a claim that the note has only one subject: the list
 * is ORDERED as written, so the colour is stable, and the subject filter (which is a membership
 * test) still finds the note under every subject it declares. */
function colorFor(node) {
  if (node && node.status === "contested") {
    return "#c0392b"; // warm red
  }
  if (node && node.orphan) {
    return "#9aa3ad"; // muted grey
  }
  const subjects = (node && node.subjects) || [];
  const key = subjects.length > 0 ? String(subjects[0]) : "index";
  let hash = 0;
  for (let i = 0; i < key.length; i++) {
    hash = (hash * 31 + key.charCodeAt(i)) | 0;
  }
  const hue = Math.abs(hash) % 360;
  return "hsl(" + hue + ", 62%, 48%)";
}

/* Initialize one graph container from its data-graph-src URL. */
function initGraph(el) {
  const src = el.dataset.graphSrc;
  if (!src) {
    return;
  }
  fetch(src)
    .then(function (resp) {
      // Guard resp.ok so a 5xx error body (which has no `nodes`) shows the load-error message
      // rather than the misleading "empty KB" empty-state.
      if (!resp.ok) {
        throw new Error("graph request failed: " + resp.status);
      }
      return resp.json();
    })
    .then(function (data) {
      const nodes = (data && data.nodes) || [];
      const edges = (data && data.edges) || [];

      if (nodes.length === 0) {
        // Empty-state: textContent (never innerHTML) so it can't inject markup.
        el.classList.add("graph-empty");
        el.textContent = "No notes to graph yet.";
        return;
      }

      if (data.truncated) {
        // A textContent banner inserted as a sibling BEFORE the canvas (NOT innerHTML).
        const banner = document.createElement("div");
        banner.className = "graph-banner";
        banner.textContent =
          "Showing " +
          nodes.length +
          " of " +
          data.node_total +
          " notes (capped). Narrow with the subject filter above to see a focused graph.";
        el.parentNode.insertBefore(banner, el);
      }

      // Auto-frame the graph to the canvas ONCE, when its layout first settles — force-graph does
      // NOT zoom-to-fit by default, so without this the graph clusters small in the centre. The
      // `framed` guard keeps it to a single fit so a later tick (or a window resize) can't yank a
      // user's manual zoom/pan afterwards.
      let framed = false;
      function frameGraph() {
        if (framed) {
          return;
        }
        framed = true;
        g.zoomToFit(400, 40); // 400ms ease, 40px padding
      }

      // force-graph 1.51.4 is a CLASS constructor; it wants link arrays under the key "links" and
      // references nodes by the id field. Our edges already carry {source, target} as rel_path ids.
      const g = new ForceGraph(el)
        .graphData({ nodes: nodes, links: edges })
        .nodeId("id")
        .nodeLabel(function (n) {
          return escapeHtml(n.title || n.id);
        })
        .nodeColor(function (n) {
          return colorFor(n);
        })
        .nodeRelSize(5)
        .linkDirectionalArrowLength(3)
        .onNodeClick(function (n) {
          window.location.href = noteHref(n.id);
        })
        // Bound the simulation so it settles fast on a small KB, then frame on engine stop.
        .cooldownTicks(120)
        .onEngineStop(frameGraph)
        .width(el.clientWidth)
        .height(el.clientHeight);

      // Fallback frame: cover a graph that settles before onEngineStop is wired, or a degenerate
      // single-node local graph that never emits the event. Idempotent via the `framed` guard.
      window.setTimeout(frameGraph, 1500);

      // Re-fit the canvas to the container on resize so it stays full-width — coalesced via rAF so a
      // resize drag relayouts at most once per frame, not on every event.
      let resizePending = false;
      window.addEventListener("resize", function () {
        if (resizePending) {
          return;
        }
        resizePending = true;
        window.requestAnimationFrame(function () {
          resizePending = false;
          g.width(el.clientWidth).height(el.clientHeight);
        });
      });

      // "Reset view" control overlaid on the canvas: re-frame the whole graph on demand, so after
      // panning/zooming around the user can snap straight back to the initial auto-fit framing. It
      // calls g.zoomToFit DIRECTLY (not the one-shot, guarded frameGraph) because this is an
      // explicit user action — and since panning/zooming only move the CAMERA (node positions are
      // unchanged), zoomToFit(400, 40) restores exactly the same fitted view as the initial load.
      const resetBtn = document.createElement("button");
      resetBtn.type = "button";
      resetBtn.className = "graph-reset";
      resetBtn.textContent = "Reset view";
      resetBtn.title = "Re-fit the whole graph to the view (the initial auto-fit)";
      resetBtn.addEventListener("click", function () {
        g.zoomToFit(400, 40);
      });
      el.appendChild(resetBtn);
    })
    .catch(function () {
      el.classList.add("graph-empty");
      el.textContent = "Could not load the graph.";
    });
}

document.addEventListener("DOMContentLoaded", function () {
  // Both the global /graph page and every note's Connections embed share this one initializer.
  document.querySelectorAll("[data-graph-src]").forEach(initGraph);
});
