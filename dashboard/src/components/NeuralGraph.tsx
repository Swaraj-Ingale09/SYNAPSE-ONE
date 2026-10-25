import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type ForceLink,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from "d3-force";
import type { Graph, GraphNode, GraphEdge, NodeKind, Truncation } from "../api";

/**
 * The "neural network" is a force-directed rendering of the real system graph.
 *
 * Design commitments, mirroring the backend's:
 *
 * - **Positions are simulated, existence is not.** d3-force decides where a
 *   node sits; the backend decides whether it exists at all. No node is ever
 *   added here for visual balance.
 * - **One simulation, many snapshots.** The simulation is created once and fed
 *   fresh node/link arrays on every poll. Rebuilding it per snapshot reheated
 *   the layout from scratch every ~2.5s (visible churn + needless CPU), so now
 *   nodes persist and a small alpha nudge lets the layout evolve in place.
 * - **Hover reads the node's own `details`**, which the backend populated from
 *   the row itself (payload, provenance, timings). Nothing in this component
 *   invents a field.
 * - **Click expands, Escape restores.** Handled by the parent via
 *   `onToggleFocus`, so the same graph instance is reused — the layout is not
 *   rebuilt, which is why expanding feels like a camera move, not a reload.
 */

const KIND_COLOR: Record<NodeKind, string> = {
  job: "#3882f6",
  worker: "#22d3ee",
  document: "#a78bfa",
  model: "#f472b6",
  chunk: "#34d399",
};

const EDGE_COLOR: Record<string, string> = {
  claimed: "rgba(34, 211, 238, 0.35)",
  used: "rgba(244, 114, 182, 0.35)",
  produced: "rgba(167, 139, 250, 0.4)",
  contains: "rgba(52, 211, 153, 0.25)",
};

// Alpha level used when a snapshot arrives: enough for new nodes to settle in,
// not enough to disturb a graph that is already calm. The old per-poll
// alpha(0.9) reheated everything, so the layout jittered even when the system
// state hadn't changed at all.
const SNAPSHOT_ALPHA = 0.25;

interface SimNode extends SimulationNodeDatum {
  id: string;
  kind: NodeKind;
  label: string;
  status: string | null;
  weight: number;
  data: GraphNode;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  kind: string;
}

interface Props {
  graph: Graph;
  focused: boolean;
  onToggleFocus: () => void;
}

interface DragState {
  pointerId: number;
  node: SimNode | null;
  lastX: number;
  lastY: number;
  moved: number;
}

export default function NeuralGraph({ graph, focused, onToggleFocus }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const simRef = useRef<Simulation<SimNode, SimLink> | null>(null);
  const linkForceRef = useRef<ForceLink<SimNode, SimLink> | null>(null);
  const nodesRef = useRef<Map<string, SimNode>>(new Map());
  const linksRef = useRef<SimLink[]>([]);
  const hoverRef = useRef<SimNode | null>(null);
  const camRef = useRef({ scale: 1, tx: 0, ty: 0 });
  const dragRef = useRef<DragState | null>(null);
  const drawPendingRef = useRef(false);

  const [hovered, setHovered] = useState<{
    node: GraphNode;
    x: number;
    y: number;
    stageW: number;
    stageH: number;
  } | null>(null);
  const [dragging, setDragging] = useState(false);

  // --- drawing ---------------------------------------------------------------
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;

    const dpr = window.devicePixelRatio || 1;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (w === 0 || h === 0) return;

    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const { scale, tx, ty } = camRef.current;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.translate(w / 2 + tx, h / 2 + ty);
    ctx.scale(scale, scale);

    const nodes = nodesRef.current;
    const links = linksRef.current;
    const hover = hoverRef.current;

    // Edges first.
    for (const link of links) {
      const s = typeof link.source === "object" ? (link.source as SimNode) : nodes.get(link.source as string);
      const t = typeof link.target === "object" ? (link.target as SimNode) : nodes.get(link.target as string);
      // x/y may legitimately be 0, so compare against null explicitly — the
      // old truthiness check silently dropped every edge touching an axis.
      if (s?.x == null || s.y == null || t?.x == null || t.y == null) continue;

      const touching = hover && (s.id === hover.id || t.id === hover.id);
      ctx.strokeStyle = touching ? "rgba(148, 197, 255, 0.85)" : EDGE_COLOR[link.kind] ?? "rgba(125,141,176,0.2)";
      ctx.lineWidth = touching ? 1.6 : 0.8;
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      // A slight curve reads as "neural" without being decorative noise.
      const mx = (s.x + t.x) / 2 + (s.y - t.y) * 0.08;
      const my = (s.y + t.y) / 2 + (t.x - s.x) * 0.08;
      ctx.quadraticCurveTo(mx, my, t.x, t.y);
      ctx.stroke();
    }

    // Nodes.
    for (const node of nodes.values()) {
      if (node.x == null || node.y == null) continue;
      const color = KIND_COLOR[node.kind];
      const r = 4 + node.weight * 2.4;
      const isActive = node.status === "running" || node.status === "active" || node.status === "indexed";
      const isHover = hover?.id === node.id;

      // The halo is decoration keyed off the node's REAL status; it is not a
      // measurement and is never labeled as one.
      if (isActive || isHover) {
        const halo = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, r * 3.4);
        halo.addColorStop(0, color + "66");
        halo.addColorStop(1, color + "00");
        ctx.fillStyle = halo;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r * 3.4, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.fillStyle = color;
      ctx.globalAlpha = node.status === "completed" || node.status === "idle" ? 0.55 : 1;
      ctx.beginPath();
      ctx.arc(node.x, node.y, isHover ? r + 1.6 : r, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;

      if (isHover) {
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.arc(node.x, node.y, r + 3.4, 0, Math.PI * 2);
        ctx.stroke();
      }

      // Labels only when zoomed in or when the node is significant.
      if (scale > 1.1 || node.weight >= 2.5 || isHover) {
        ctx.fillStyle = "rgba(219, 230, 255, 0.72)";
        ctx.font = `${10 / Math.max(scale, 1)}px "JetBrains Mono", monospace`;
        ctx.textAlign = "center";
        ctx.fillText(node.label.slice(0, 26), node.x, node.y + r + 11);
      }
    }
  }, []);

  // Draw on demand rather than in a free-running 60fps loop: simulation ticks
  // and pointer/camera handlers flag the need, so an idle graph costs nothing.
  const requestDraw = useCallback(() => {
    if (drawPendingRef.current) return;
    drawPendingRef.current = true;
    requestAnimationFrame(() => {
      drawPendingRef.current = false;
      draw();
    });
  }, [draw]);

  // --- one long-lived simulation ----------------------------------------------
  useEffect(() => {
    const link = forceLink<SimNode, SimLink>([])
      .id((d) => d.id)
      .distance((l) => (l.kind === "contains" ? 46 : 92))
      .strength(0.35);
    linkForceRef.current = link;

    const sim = forceSimulation<SimNode>([])
      .force("link", link)
      .force("charge", forceManyBody<SimNode>().strength(-190).distanceMax(480))
      .force("center", forceCenter(0, 0))
      .force("collide", forceCollide<SimNode>((d) => 9 + d.weight * 3.2))
      .alphaDecay(0.022)
      .on("tick", requestDraw)
      // Driven only by explicit restarts (snapshot ingest, drags); letting it
      // free-run at rest would tick the timer for no visual change.
      .stop();

    simRef.current = sim;
    return () => {
      sim.stop();
      simRef.current = null;
      linkForceRef.current = null;
    };
  }, [requestDraw]);

  // --- ingest a new snapshot, preserving positions --------------------------
  useEffect(() => {
    const existing = nodesRef.current;
    const next = new Map<string, SimNode>();

    for (const node of graph.nodes) {
      const prior = existing.get(node.id);
      next.set(node.id, {
        id: node.id,
        kind: node.kind,
        label: node.label,
        status: node.status,
        weight: node.weight,
        data: node,
        // Reuse the old position so a refresh does not scramble the layout.
        // New nodes get undefined here; initializeNodes() then seeds them near
        // the center and the forces push them out — they "arrive" instead of
        // popping in at a random edge.
        x: prior?.x,
        y: prior?.y,
        vx: prior?.vx,
        vy: prior?.vy,
      });
    }
    nodesRef.current = next;

    const links: SimLink[] = graph.edges
      .filter((e) => next.has(e.source) && next.has(e.target))
      .map((e: GraphEdge) => ({ source: e.source, target: e.target, kind: e.kind }));
    linksRef.current = links;

    // Re-point an in-flight drag at the refreshed node object, so the pin
    // keeps applying to the instance the simulation is actually moving.
    const drag = dragRef.current;
    if (drag?.node) {
      const refreshed = next.get(drag.node.id);
      if (refreshed) {
        refreshed.fx = drag.node.fx;
        refreshed.fy = drag.node.fy;
        drag.node = refreshed;
      } else {
        // The dragged node vanished between snapshots.
        drag.node = null;
      }
    }

    const sim = simRef.current;
    const linkForce = linkForceRef.current;
    if (!sim || !linkForce) return;

    // Order matters: sim.nodes() re-initializes the link force against the NEW
    // node set. Attaching the fresh links first would resolve their ids against
    // the previous snapshot's nodes — a brand-new id would come back undefined
    // and forceLink would throw while rebuilding its degree counts.
    sim.nodes([...next.values()]);
    linkForce.links(links);

    sim.alpha(Math.max(sim.alpha(), SNAPSHOT_ALPHA)).restart();
    requestDraw();
  }, [graph, requestDraw]);

  // --- redraw on resize -------------------------------------------------------
  useEffect(() => {
    const onResize = () => requestDraw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [requestDraw]);

  // --- pointer interaction ----------------------------------------------------
  const toWorld = (clientX: number, clientY: number) => {
    const wrap = wrapRef.current;
    if (!wrap) return { x: 0, y: 0 };
    const rect = wrap.getBoundingClientRect();
    const { scale, tx, ty } = camRef.current;
    return {
      x: (clientX - rect.left - rect.width / 2 - tx) / scale,
      y: (clientY - rect.top - rect.height / 2 - ty) / scale,
    };
  };

  const pickNode = (clientX: number, clientY: number): SimNode | null => {
    const { x, y } = toWorld(clientX, clientY);
    let best: SimNode | null = null;
    let bestDist = 16 / camRef.current.scale;
    for (const node of nodesRef.current.values()) {
      if (node.x == null || node.y == null) continue;
      const d = Math.hypot(node.x - x, node.y - y);
      if (d < bestDist) {
        bestDist = d;
        best = node;
      }
    }
    return best;
  };

  const handlePointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const node = pickNode(e.clientX, e.clientY);
    if (node) {
      // Pin immediately so the node does not drift between press and first move.
      node.fx = node.x;
      node.fy = node.y;
      simRef.current?.alphaTarget(0.25).restart();
    } else {
      // Panning should not leave a stale tooltip behind.
      hoverRef.current = null;
      setHovered(null);
    }
    // Capture keeps move/up flowing when the pointer leaves the canvas
    // mid-gesture; without it a release outside never unpinns the node and the
    // next bare mousemove resumes the drag with no button held.
    e.currentTarget.setPointerCapture(e.pointerId);
    dragRef.current = { pointerId: e.pointerId, node, lastX: e.clientX, lastY: e.clientY, moved: 0 };
    setDragging(true);
    requestDraw();
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (drag && drag.pointerId === e.pointerId) {
      const dx = e.clientX - drag.lastX;
      const dy = e.clientY - drag.lastY;
      drag.moved += Math.abs(dx) + Math.abs(dy);
      if (drag.node) {
        // Dragging a node moves the node (fx/fy), never the camera.
        const world = toWorld(e.clientX, e.clientY);
        drag.node.fx = world.x;
        drag.node.fy = world.y;
      } else {
        camRef.current.tx += dx;
        camRef.current.ty += dy;
      }
      drag.lastX = e.clientX;
      drag.lastY = e.clientY;
      requestDraw();
      return;
    }

    const node = pickNode(e.clientX, e.clientY);
    hoverRef.current = node;
    if (node) {
      const rect = wrapRef.current?.getBoundingClientRect();
      setHovered({
        node: node.data,
        x: e.clientX - (rect?.left ?? 0) + 16,
        y: e.clientY - (rect?.top ?? 0) + 16,
        stageW: rect?.width ?? 0,
        stageH: rect?.height ?? 0,
      });
    } else {
      setHovered(null);
    }
    requestDraw();
  };

  // Shared cleanup for pointerup, pointercancel and lostpointercapture — the
  // last two cover window-blur and OS-level gesture interruption.
  const endDrag = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    simRef.current?.alphaTarget(0);
    if (drag.node) {
      // Release a dragged node back to the simulation.
      drag.node.fx = null;
      drag.node.fy = null;
      simRef.current?.alpha(0.3).restart();
      requestDraw();
    }
  };

  const handlePointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const drag = dragRef.current;
    // A click (not a pan) on empty space toggles focus; a stray pointerup from
    // another touch must not count as this drag's click.
    const clickedEmpty =
      drag != null && drag.pointerId === e.pointerId && drag.node == null && drag.moved < 4;
    endDrag(e);
    if (clickedEmpty) onToggleFocus();
  };

  const handlePointerLeave = () => {
    hoverRef.current = null;
    setHovered(null);
    requestDraw();
  };

  const handleWheel = (e: React.WheelEvent) => {
    const cam = camRef.current;
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    cam.scale = Math.min(4, Math.max(0.25, cam.scale * factor));
    requestDraw();
  };

  // The backend caps how many nodes it returns per kind; saying so beats
  // implying the rendered subset is the whole system.
  const jobTrunc: Truncation | undefined = graph.truncated["jobs"];
  const docTrunc: Truncation | undefined = graph.truncated["documents"];
  const truncationNote = [
    jobTrunc && jobTrunc.total != null && jobTrunc.total > jobTrunc.shown
      ? `showing ${jobTrunc.shown} of ${jobTrunc.total.toLocaleString()} jobs`
      : null,
    docTrunc && docTrunc.total != null && docTrunc.total > docTrunc.shown
      ? `${docTrunc.shown} of ${docTrunc.total.toLocaleString()} docs`
      : null,
  ]
    .filter((part): part is string => part != null)
    .join(" · ");

  return (
    <div ref={wrapRef} className="graph-stage">
      <canvas
        ref={canvasRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={endDrag}
        onLostPointerCapture={endDrag}
        onPointerLeave={handlePointerLeave}
        onWheel={handleWheel}
        style={{ cursor: dragging ? "grabbing" : hovered ? "pointer" : "grab" }}
      />

      {hovered && (
        <Tooltip
          node={hovered.node}
          x={hovered.x}
          y={hovered.y}
          stageW={hovered.stageW}
          stageH={hovered.stageH}
        />
      )}

      <div className="graph-hint">
        <div>
          <kbd>drag</kbd> node to pin · <kbd>drag</kbd> background to pan · <kbd>scroll</kbd> zoom
        </div>
        <div>
          <kbd>click</kbd> background to {focused ? "restore" : "expand"} · <kbd>esc</kbd> to restore
        </div>
        <div className="graph-hint-real">
          every node is a real row
          {truncationNote ? ` — ${truncationNote}` : ""}
        </div>
      </div>
    </div>
  );
}

/**
 * Hover panel. Renders only fields the backend actually sent, so it cannot
 * display a metric that was never measured. The position is clamped to the
 * stage after layout so a node near the edge cannot push it off-screen.
 */
function Tooltip({
  node,
  x,
  y,
  stageW,
  stageH,
}: {
  node: GraphNode;
  x: number;
  y: number;
  stageW: number;
  stageH: number;
}) {
  const color = KIND_COLOR[node.kind];
  const entries = Object.entries(node.details).filter(([, v]) => v !== null && v !== undefined);
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: x, top: y });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const left = clamp(x, 4, stageW - el.offsetWidth - 4);
    const top = clamp(y, 4, stageH - el.offsetHeight - 4);
    setPos({ left, top });
  }, [x, y, stageW, stageH]);

  return (
    <div ref={ref} className="tooltip" style={{ left: pos.left, top: pos.top }}>
      <span className="kind" style={{ background: color + "22", color }}>
        {node.kind}
      </span>
      {node.status && <span className="kind status">{node.status}</span>}
      <h4>{node.label}</h4>
      {entries.length > 0 ? (
        <dl className="kv">
          {entries.map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt>{key.replace(/_/g, " ")}</dt>
              <dd>{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        // Honest empty state: the backend sent no details for this row.
        <div className="tooltip-empty">no details recorded for this row</div>
      )}
    </div>
  );
}

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value));
}

function formatValue(value: unknown): string {
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") {
    return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(3);
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
