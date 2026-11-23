import type { TelemetryEvent } from '../types';
import { eventIdentity, eventTimestamp, telemetryFlows } from './telemetryWorkspace';

export const FLOW_PARTICLE_LIMIT = 32;
export const FLOW_FRESH_MS = 10_000;
export const FLOW_TRAVEL_MS = 1_800;
const SEEN_LIMIT = 1_200;
export type FlowSeen = { notBefore: number; ids: Set<string> };

/** First replay establishes history, not live activity. Suppressed arrivals are consumed too. */
export function takeFlowArrivals(state: FlowSeen, events: TelemetryEvent[], now: number, enabled: boolean) {
  const fresh: TelemetryEvent[] = [];
  for (const event of events) {
    const id = eventIdentity(event);
    if (state.ids.has(id)) continue;
    state.ids.add(id);
    const age = now - eventTimestamp(event);
    if (enabled && eventTimestamp(event) > state.notBefore && age >= 0 && age <= FLOW_FRESH_MS) fresh.push(event);
  }
  while (state.ids.size > SEEN_LIMIT) state.ids.delete(state.ids.values().next().value!);
  return fresh.slice(-FLOW_PARTICLE_LIMIT);
}

export type FlowNode = { id: string; x: number; y: number; width: number; count: number };
export type FlowEdge = ReturnType<typeof telemetryFlows>[number] & { path: string };
function lane(id: string): number {
  if (id === 'youtube_api' || id === 'auto_sync') return 0;
  if (['sync_engine', 'sync_service', 'backend', 'ui'].includes(id)) return 1;
  return 2;
}

/** Placement groups labels for readability only; only observed pairs create edges. */
export function buildFlowMap(events: TelemetryEvent[], width: number) {
  const flows = telemetryFlows(events).slice(0, 8);
  const counts = new Map<string, number>();
  for (const flow of flows) {
    counts.set(flow.source, (counts.get(flow.source) ?? 0) + flow.count);
    if (flow.target !== flow.source) counts.set(flow.target, (counts.get(flow.target) ?? 0) + flow.count);
  }
  const ids = [...counts.keys()].sort((a, b) => lane(a) - lane(b) || a.localeCompare(b));
  const mobile = width < 600;
  const columns = mobile ? 2 : 3;
  const groups: string[][] = Array.from({ length: columns }, () => []);
  ids.forEach((id, index) => groups[mobile ? index % columns : lane(id)].push(id));
  const rows = Math.max(1, ...groups.map((group) => group.length));
  const height = Math.max(mobile ? 310 : 320, rows * 104 + 70);
  const nodeWidth = Math.min(mobile ? 136 : 180, (width - 42) / columns - 10);
  const nodes: FlowNode[] = groups.flatMap((group, column) => group.map((id, index) => ({
    id, width: nodeWidth, x: 18 + nodeWidth / 2 + column * (width - 36 - nodeWidth) / (columns - 1),
    y: height / 2 + (index - (group.length - 1) / 2) * 104, count: counts.get(id)!,
  })));
  const lookup = new Map(nodes.map((node) => [node.id, node]));
  const edges: FlowEdge[] = flows.map((flow) => {
    const from = lookup.get(flow.source)!, to = lookup.get(flow.target)!;
    let path: string;
    if (from.id === to.id) {
      path = `M ${from.x - 26} ${from.y - 30} C ${from.x - 70} ${from.y - 82}, ${from.x + 70} ${from.y - 82}, ${from.x + 26} ${from.y - 30}`;
    } else if (from.x === to.x) {
      const side = from.x > width / 2 ? -1 : 1;
      const x = from.x + side * from.width / 2;
      const reciprocal = flows.some((other) => other.source === flow.target && other.target === flow.source);
      const bend = reciprocal && from.y > to.y ? 68 : 32;
      path = `M ${x} ${from.y} C ${x + side * bend} ${from.y}, ${x + side * bend} ${to.y}, ${x} ${to.y}`;
    } else {
      const direction = to.x > from.x ? 1 : -1;
      const x1 = from.x + direction * from.width / 2, x2 = to.x - direction * to.width / 2;
      const bend = (x1 + x2) / 2;
      // Reciprocal routes get separate tracks; never reverse the recorded pair.
      const offset = flows.some((other) => other.source === flow.target && other.target === flow.source) ? direction * 9 : 0;
      path = `M ${x1} ${from.y + offset} C ${bend} ${from.y + offset}, ${bend} ${to.y + offset}, ${x2} ${to.y + offset}`;
    }
    return { ...flow, path };
  });
  return { nodes, edges, height, totalRoutes: telemetryFlows(events).length };
}
