import {
  Background,
  BackgroundVariant,
  BaseEdge,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useEffect, useMemo, useState } from 'react';
import type { Graph } from '../api/types';
import { collapse } from '../graph/collapse';
import { place, type Laid, type Placed, type Point } from '../graph/elk';
import { kindLabel, MOVE, nodeById } from '../graph/moves';

type StepData = { id: string; header: string; meta: string; state: 'active' | 'next' | 'seen' | 'idle'; consulted: boolean; virtual: boolean };
type GroupData = { size: number };

function StepNode({ data }: NodeProps<Node<StepData>>) {
  if (data.virtual) {
    return (
      <div className="map-end">
        <Handle type="target" position={Position.Left} />
        END
      </div>
    );
  }
  return (
    <div className={`map-step map-${data.state} ${data.consulted ? 'map-consulted' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <span className="card-h">
        {data.state === 'active' ? '● ' : ''}
        {data.header}
        <span style={{ flex: '1 1 auto' }} />
        {data.state === 'next' ? 'can go' : ''}
      </span>
      <span className="card-t">{data.id}</span>
      <span className="card-m">{data.meta}</span>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function GroupNode({ data }: NodeProps<Node<GroupData>>) {
  return (
    <div className="map-group">
      <Handle type="target" position={Position.Left} />
      <div className="map-group-h">
        <span>{data.size} steps</span>
        <span className="map-any">↔ any to any</span>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

type LineData = { points: Point[]; text: string; lit: boolean };

/** A line along ELK's own route, with its label at the middle of its longest run, shown only when lit. */
function RoutedEdge({ data, style, markerEnd }: EdgeProps<Edge<LineData>>) {
  const points = data?.points ?? [];
  if (points.length < 2) return null;
  const path = points.map((p, i) => `${i ? 'L' : 'M'} ${p.x} ${p.y}`).join(' ');
  let best = 0;
  let mid = points[0] as Point;
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1] as Point;
    const b = points[i] as Point;
    const length = Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
    if (length > best) {
      best = length;
      mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    }
  }
  return (
    <>
      <BaseEdge path={path} style={style} markerEnd={markerEnd} />
      {data?.lit && (
        <EdgeLabelRenderer>
          <span className="map-label" style={{ transform: `translate(-50%, -50%) translate(${mid.x}px, ${mid.y}px)` }}>
            {data.text}
          </span>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { step: StepNode, cluster: GroupNode };
const edgeTypes = { routed: RoutedEdge };

/**
 * Option C: the whole agent. Steps that all hand over to one another are one group, so the map
 * draws the moves between groups and steps, not every move (`collapse`); ELK places it (`place`).
 * The moves out of `focus` are lit; `seen` are the steps this run has been through.
 */
export function MapView({ graph, focus, seen = [], onPick }: {
  graph: Graph;
  focus?: string;
  seen?: string[];
  onPick: (step: string) => void;
}) {
  const lines = useMemo(() => collapse(graph), [graph]);
  const [laid, setLaid] = useState<Laid | null>(null);
  useEffect(() => {
    let live = true;
    place(graph, lines).then((placed) => live && setLaid(placed));
    return () => {
      live = false;
    };
  }, [graph, lines]);

  const groupOf = new Map(graph.groups.flatMap((g) => g.steps.map((s) => [s, g.id] as const)));
  const focusKeys = new Set([focus, focus && groupOf.get(focus)].filter(Boolean));
  const next = new Set(graph.edges.filter((e) => e.source === focus).map((e) => e.target));
  const byId = nodeById(graph);

  if (!laid) return <p className="empty">Laying out the graph…</p>;
  const { at, routes } = laid;

  const nodes: Node[] = [
    ...graph.groups.map((g) => {
      const p = at[g.id] as Placed;
      return { id: g.id, type: 'cluster', position: { x: p.x, y: p.y }, style: { width: p.w, height: p.h }, data: { size: g.steps.length }, selectable: false };
    }),
    ...graph.nodes.map((n) => {
      const p = at[n.id] as Placed;
      const state: StepData['state'] = n.id === focus ? 'active' : next.has(n.id) ? 'next' : seen.includes(n.id) ? 'seen' : 'idle';
      const plural = (k: number, word: string) => `${k} ${word}${k === 1 ? '' : 's'}`;
      const meta = [n.fields.length && plural(n.fields.length, 'field'), n.operations.length && plural(n.operations.length, 'op'), n.offers.length && `offers ${n.offers.join(', ')}`]
        .filter(Boolean)
        .join(' · ');
      return {
        id: n.id,
        type: 'step',
        position: { x: p.x, y: p.y },
        parentId: p.parent,
        extent: p.parent ? ('parent' as const) : undefined,
        data: { id: n.id, header: kindLabel(byId.get(n.id) ?? n), meta: meta || '—', state, consulted: n.roles.includes('consulted'), virtual: n.virtual },
      };
    }),
  ];
  const edges: Edge<LineData>[] = lines.map((l) => {
    const lit = focusKeys.has(l.source);
    const dash = l.kind === 'consult' || l.kind === 'handback' ? '6 5' : l.kind === 'prerequisite' || l.kind === 'return' ? '2 5' : undefined;
    return {
      id: l.id,
      source: l.source,
      target: l.target,
      type: 'routed',
      data: { points: routes[l.id] ?? [], text: l.count > 1 ? `${MOVE[l.kind].label} · ${l.count}` : MOVE[l.kind].label, lit },
      style: { stroke: lit ? 'var(--yellow)' : '#4a463f', strokeWidth: lit ? 1.8 : 1.3, strokeDasharray: dash },
      markerEnd: { type: MarkerType.ArrowClosed, color: lit ? '#fbe566' : '#4a463f', width: 14, height: 14 },
      zIndex: lit ? 2 : 1,
    };
  });

  return (
    <div className="map">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        fitView
        fitViewOptions={{ padding: 0.12, minZoom: 0.55 }}
        nodesDraggable={false}
        nodesConnectable={false}
        colorMode="dark"
        onNodeClick={(_, node) => node.type === 'step' && node.id !== '$end' && onPick(node.id)}
        minZoom={0.3}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="#2e2b26" />
      </ReactFlow>
      <span className="map-note">every step can end the call · goodbyes are not drawn</span>
    </div>
  );
}
