import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { TelemetryEvent } from '../types';
import { eventIdentity, telemetryFailed, telemetrySourceLabel } from './telemetryWorkspace';
import { buildFlowMap, FLOW_PARTICLE_LIMIT, FLOW_TRAVEL_MS, takeFlowArrivals, type FlowSeen } from './telemetryFlowMap';
import './telemetry-flow-map.css';

type Props = { events: TelemetryEvent[]; arrivals: TelemetryEvent[]; paused: boolean; connected: boolean; journalUnavailable?: boolean; selectedRoute?: string; onSelectRoute: (key: string) => void };
type Particle = { id: string; route: string; failed: boolean; expires: number };
function beginMotion(node: SVGAnimationElement | null) { node?.beginElement(); }

/** Controlled visualization: no transport, polls, domain state or synthetic production motion. */
export function TelemetryFlowMapChart({ events, arrivals, paused, connected, journalUnavailable = false, selectedRoute, onSelectRoute }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const seen = useRef<FlowSeen>({ notBefore: Date.now(), ids: new Set() });
  const wasEnabled = useRef(false);
  const [width, setWidth] = useState(960);
  const [hidden, setHidden] = useState(() => document.hidden);
  const [reduced, setReduced] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [particles, setParticles] = useState<Particle[]>([]);
  const marker = useId().replace(/:/g, '');
  const model = useMemo(() => buildFlowMap(events, width), [events, width]);
  const enabled = !paused && !hidden && !reduced && connected && !journalUnavailable;
  useEffect(() => {
    const measure = () => setWidth(Math.max(240, container.current?.clientWidth ?? 960));
    const observer = new ResizeObserver(measure);
    if (container.current) observer.observe(container.current);
    measure();
    const visibility = () => setHidden(document.hidden);
    const preference = window.matchMedia('(prefers-reduced-motion: reduce)');
    const motion = () => setReduced(preference.matches);
    document.addEventListener('visibilitychange', visibility);
    preference.addEventListener('change', motion);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', visibility);
      preference.removeEventListener('change', motion);
    };
  }, []);
  useEffect(() => {
    const now = Date.now();
    if (enabled !== wasEnabled.current) seen.current.notBefore = now;
    wasEnabled.current = enabled;
    const fresh = takeFlowArrivals(seen.current, arrivals, now, enabled);
    if (!enabled) { setParticles([]); return; }
    const added = fresh.flatMap((event) => {
      const edge = model.edges.find((edge) => edge.source === event.source && edge.target === event.target);
      return edge ? [{ id: eventIdentity(event), route: edge.key, failed: telemetryFailed(event), expires: now + FLOW_TRAVEL_MS }] : [];
    });
    if (added.length) setParticles((current) => [...current.filter((item) => item.expires > now), ...added].slice(-FLOW_PARTICLE_LIMIT));
  }, [arrivals, enabled, model.edges]);
  useEffect(() => {
    if (!particles.length) return;
    const timer = window.setTimeout(() => setParticles((current) => current.filter((item) => item.expires > Date.now())),
      Math.max(0, Math.min(...particles.map((item) => item.expires)) - Date.now()) + 20);
    return () => window.clearTimeout(timer);
  }, [particles]);
  const active = new Set(particles.map((particle) => particle.route));
  const motionState = paused ? 'Paused' : hidden ? 'Tab hidden' : reduced ? 'Reduced motion' : journalUnavailable ? 'Journal unavailable' : !connected ? 'Waiting for connection' : particles.length ? 'Receiving events' : 'Waiting for new events';
  return <section className="telemetry-v2-map" aria-labelledby={`${marker}-title`}>
    <div className="telemetry-v2-section-heading"><div><p className="telemetry-v2-eyebrow">Event-driven map</p><h2 id={`${marker}-title`}>Telemetry flow</h2></div>
      <span className={particles.length && enabled ? 'telemetry-v2-map-live' : ''}>{motionState}</span></div>
    <div className="telemetry-v2-map-stage" ref={container} style={{ height: model.height }} data-motion={enabled ? 'enabled' : 'disabled'}>
      <svg viewBox={`0 0 ${width} ${model.height}`} className="telemetry-v2-map-svg" aria-label="Recorded source to target routes">
        <defs><marker id={marker} markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7" fill="currentColor" /></marker></defs>
        {model.edges.map((edge) => <g key={edge.key} className={`telemetry-v2-map-edge ${active.has(edge.key) ? 'is-active' : ''} ${selectedRoute === edge.key ? 'is-selected' : ''}`}>
          <path d={edge.path} className="telemetry-v2-map-track" markerEnd={`url(#${marker})`} />
          <path d={edge.path} className="telemetry-v2-map-hit" role="button" tabIndex={0} aria-pressed={selectedRoute === edge.key}
            aria-label={`Filter route ${telemetrySourceLabel(edge.source)} to ${telemetrySourceLabel(edge.target)}`}
            onClick={() => onSelectRoute(edge.key)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelectRoute(edge.key); } }}>
            <title>{edge.source} → {edge.target} · {edge.count} events in sample</title>
          </path>
        </g>)}
        {enabled && particles.map((particle) => {
          const edge = model.edges.find((edge) => edge.key === particle.route);
          return edge && <circle key={particle.id} r="4" className={`telemetry-v2-map-particle ${particle.failed ? 'is-failed' : ''}`} aria-hidden="true">
            <animateMotion ref={beginMotion} begin="indefinite" dur={`${FLOW_TRAVEL_MS}ms`} path={edge.path} fill="freeze" />
          </circle>;
        })}
      </svg>
      {model.nodes.map((node) => {
        const receiving = model.edges.some((edge) => active.has(edge.key) && (edge.source === node.id || edge.target === node.id));
        return <div key={node.id} className={`telemetry-v2-map-node ${receiving && enabled ? 'is-active' : ''}`}
          style={{ left: `${100 * node.x / width}%`, top: node.y, width: node.width }} title={node.id}>
          <span className="telemetry-v2-map-node-dot" /><strong>{telemetrySourceLabel(node.id)}</strong><small>{node.count.toLocaleString('en')} events in sample</small>
        </div>;
      })}
      {!model.nodes.length && <p className="telemetry-v2-map-empty">The map appears when recorded events arrive.</p>}
    </div>
    <div className="telemetry-v2-map-footer"><span><i />Event <i className="is-error" />Error event</span><span>{model.edges.length} of {model.totalRoutes} recorded routes</span></div>
    <p className="telemetry-v2-note">Signals follow recorded source → target, not inferred data flow. Motion shows newly received events up to 10 seconds old; initial history stays still. Select a connection to filter the log.</p>
  </section>;
}
