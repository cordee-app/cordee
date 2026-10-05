import { useState, useEffect, useRef, useCallback } from 'react';
import { useStore } from '../store';
import { api } from '../api';
import { cn } from '../utils/cn';
import type { Task, DependencyEdge } from '../types';

interface GraphNode {
  id: number;
  title: string;
  status: string;
  project_id: number;
  stale: boolean;
  archived: boolean;
  role_id: number | null;
  model: string;
  dep_up_count: number;
  dep_down_count: number;
  primary: boolean;
}

interface GraphEdge {
  from: number;
  to: number;
  stale: boolean;
  crossProject: boolean;
  sourceTitle: string;
  targetTitle: string;
}

interface Position { x: number; y: number; cx: number; cy: number; }

interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
  positions: Map<number, Position>;
  cycleDetected: boolean;
  primaryCount: number;
  externalCount: number;
  edgeCount: number;
  width: number;
  height: number;
}

const NODE_W = 240;
const NODE_H = 96;
const COL_GAP = 100;
const ROW_GAP = 20;
const PAD_X = 60;
const PAD_Y = 60;

function projectColor(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i++) {
    hash = name.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash;
  }
  const h = Math.abs(hash) % 360;
  return `hsl(${h}, 55%, 45%)`;
}

function statusColor(status: string, stale: boolean): string {
  if (stale) return '#c67139';
  if (status === 'done') return '#56633f';
  if (status === 'running') return '#3f628f';
  if (status === 'confirmed') return '#3f628f';
  if (status === 'failed') return '#a3402f';
  return '#645c50';
}

function statusGlyph(status: string): string {
  if (status === 'done') return '✓';
  if (status === 'running') return '⏳';
  if (status === 'failed') return '✕';
  if (status === 'cancelled' || status === 'skip') return '⊘';
  if (status === 'confirmed') return '◉';
  return '○';
}

const STATUS_LABEL: Record<string, string> = {
  pending: 'Pending', confirmed: 'Confirmed', running: 'Running',
  done: 'Done', failed: 'Failed', skip: 'Skipped', cancelled: 'Cancelled',
};

function buildGraphData(tasks: Task[], depRows: DependencyEdge[]): GraphData | null {
  if (!depRows || !depRows.length) return null;

  const taskById = new Map(tasks.map(t => [t.id, t]));
  const primaryIds = new Set(tasks.map(t => t.id));
  const included = new Set(primaryIds);

  let changed = true;
  while (changed) {
    changed = false;
    for (const row of depRows) {
      if (included.has(row.task_id) || included.has(row.depends_on_id)) {
        if (!included.has(row.task_id)) { included.add(row.task_id); changed = true; }
        if (!included.has(row.depends_on_id)) { included.add(row.depends_on_id); changed = true; }
      }
    }
  }
  if (!included.size) return null;

  const nodes: GraphNode[] = [];
  for (const id of included) {
    const task = taskById.get(id);
    if (!task) continue;
    nodes.push({
      id: task.id,
      title: task.title || `Task #${task.id}`,
      status: task.status || 'pending',
      project_id: task.project_id,
      stale: false,
      archived: !!task.archived,
      role_id: task.role_id || null,
      model: task.model || '',
      dep_up_count: 0,
      dep_down_count: 0,
      primary: true,
    });
  }

  const edges: GraphEdge[] = [];
  for (const row of depRows) {
    if (!included.has(row.task_id) || !included.has(row.depends_on_id)) continue;
    edges.push({
      from: row.depends_on_id,
      to: row.task_id,
      stale: false,
      crossProject: false,
      sourceTitle: '',
      targetTitle: '',
    });
  }

  for (const e of edges) {
    const src = nodes.find(n => n.id === e.from);
    const tgt = nodes.find(n => n.id === e.to);
    if (src) src.dep_down_count++;
    if (tgt) tgt.dep_up_count++;
  }

  const indegree = new Map(nodes.map(n => [n.id, 0]));
  const outgoing = new Map(nodes.map(n => [n.id, [] as number[]]));
  for (const edge of edges) {
    if (!indegree.has(edge.to) || !outgoing.has(edge.from)) continue;
    indegree.set(edge.to, (indegree.get(edge.to) || 0) + 1);
    outgoing.get(edge.from)!.push(edge.to);
  }

  const depth = new Map<number, number>();
  const queue: number[] = [];
  for (const node of nodes) {
    if ((indegree.get(node.id) || 0) === 0) {
      queue.push(node.id);
      depth.set(node.id, 0);
    }
  }
  while (queue.length) {
    const id = queue.shift()!;
    const baseDepth = depth.get(id) || 0;
    for (const next of outgoing.get(id) || []) {
      depth.set(next, Math.max(depth.get(next) || 0, baseDepth + 1));
      indegree.set(next, (indegree.get(next) || 0) - 1);
      if ((indegree.get(next) || 0) === 0) queue.push(next);
    }
  }

  let cycleDetected = false;
  const unresolved = nodes.filter(n => !depth.has(n.id));
  if (unresolved.length) {
    cycleDetected = true;
    const baseDepth = Math.max(0, ...depth.values()) + 1;
    unresolved.forEach((node, idx) => depth.set(node.id, baseDepth + idx));
  }

  const maxDepth = Math.max(0, ...depth.values());
  const columns: GraphNode[][] = Array.from({ length: maxDepth + 1 }, () => []);
  for (const node of nodes) columns[depth.get(node.id) || 0].push(node);

  const width = PAD_X * 2 + columns.length * NODE_W + Math.max(0, columns.length - 1) * COL_GAP;
  const colHeights = columns.map(col => col.length ? (col.length * NODE_H + (col.length - 1) * ROW_GAP) : 0);
  const maxColH = Math.max(NODE_H, ...colHeights);
  const height = PAD_Y * 2 + maxColH;

  const positions = new Map<number, Position>();
  columns.forEach((col, depthIdx) => {
    const colH = col.length ? (col.length * NODE_H + (col.length - 1) * ROW_GAP) : NODE_H;
    const topOffset = PAD_Y + Math.max(0, (maxColH - colH) / 2);
    const x = PAD_X + depthIdx * (NODE_W + COL_GAP);
    col.forEach((node, idx) => {
      const y = topOffset + idx * (NODE_H + ROW_GAP);
      positions.set(node.id, { x, y, cx: x + NODE_W / 2, cy: y + NODE_H / 2 });
    });
  });

  return {
    nodes, edges, positions, cycleDetected,
    primaryCount: nodes.filter(n => n.primary).length,
    externalCount: nodes.filter(n => !n.primary).length,
    edgeCount: edges.length, width, height,
  };
}

export const DependencyGraph = () => {
  const { activeProject, tasks, roles, setModModalTaskId } = useStore();
  const [graph, setGraph] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(false);
  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 1 });
  const isPanning = useRef(false);
  const panStart = useRef({ x: 0, y: 0 });
  const panOrigin = useRef({ x: 0, y: 0 });
  const stageRef = useRef<HTMLDivElement>(null);

  const loadGraph = useCallback(async () => {
    if (!activeProject) { setGraph(null); return; }
    setLoading(true);
    try {
      const deps = await api.projects.dependencies(activeProject);
      const projectTasks = tasks.filter(t => t.project_id === activeProject);
      const data = buildGraphData(projectTasks, deps);
      setGraph(data);
    } catch { setGraph(null); }
    setLoading(false);
  }, [activeProject, tasks]);

  useEffect(() => { loadGraph(); }, [loadGraph]);

  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;

    const onMouseDown = (e: MouseEvent) => {
      if ((e.target as HTMLElement).closest('.graph-node, button')) return;
      isPanning.current = true;
      panStart.current = { x: e.clientX, y: e.clientY };
      panOrigin.current = { x: transform.x, y: transform.y };
      stage.style.cursor = 'grabbing';
      e.preventDefault();
    };
    const onMouseMove = (e: MouseEvent) => {
      if (!isPanning.current) return;
      const dx = e.clientX - panStart.current.x;
      const dy = e.clientY - panStart.current.y;
      setTransform({ x: panOrigin.current.x + dx, y: panOrigin.current.y + dy, scale: transform.scale });
    };
    const onMouseUp = () => { isPanning.current = false; stage.style.cursor = ''; };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const delta = e.deltaY > 0 ? 0.9 : 1.1;
      const newScale = Math.min(Math.max(transform.scale * delta, 0.3), 3);
      const rect = stage.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const scaleChange = newScale / transform.scale;
      setTransform({
        x: mx - (mx - transform.x) * scaleChange,
        y: my - (my - transform.y) * scaleChange,
        scale: newScale,
      });
    };

    stage.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    stage.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      stage.removeEventListener('mousedown', onMouseDown);
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseup', onMouseUp);
      stage.removeEventListener('wheel', onWheel);
    };
  }, [transform]);

  useEffect(() => { setTransform({ x: 0, y: 0, scale: 1 }); }, [graph]);

  if (!activeProject) {
    return (
      <div className="dependency-graph p-0 flex flex-col h-full">
        <div className="graph-empty-note p-10 text-center text-muted">
          <strong className="block text-base mb-2 text-soft">Pick a project from the sidebar</strong>
          <p className="text-sm max-w-[400px] mx-auto leading-[1.5]">The dependency graph lays out the selected project as a topological DAG and keeps upstream / downstream context from connected tasks.</p>
        </div>
      </div>
    );
  }

  if (loading) {
    return <div className="dependency-graph p-0 flex flex-col h-full"><div className="graph-empty-note p-10 text-center text-muted"><strong className="block text-base mb-2 text-soft">Loading graph...</strong></div></div>;
  }

  if (!graph || !graph.nodes.length) {
    return (
      <div className="dependency-graph p-0 flex flex-col h-full">
        <div className="graph-empty-note p-10 text-center text-muted">
          <strong className="block text-base mb-2 text-soft">No graph to display</strong>
          <p className="text-sm max-w-[400px] mx-auto leading-[1.5]">Add dependencies to tasks in this project to build the DAG.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="dependency-graph p-0 flex flex-col h-full">
      <div className="graph-toolbar flex items-center gap-3 px-4 py-2 border-b border-default flex-shrink-0">
        <span className="graph-summary text-sm text-soft flex-1">
          {graph.nodes.length} nodes · {graph.edgeCount} edges
          {graph.primaryCount} project · {graph.externalCount} external
        </span>
        {graph.cycleDetected && (
          <span className="graph-cycle-warn text-sm+ text-status-pending bg-status-pending-bg px-2 py-0.5 rounded">{'\u26A0'} Cycles detected</span>
        )}
        <button data-tip="Reset the graph zoom and position" className="btn-xs" onClick={() => setTransform({ x: 0, y: 0, scale: 1 })}>Reset</button>
      </div>

      <div
        className="graph-viewport flex-1 overflow-hidden relative"
        style={{ background: 'radial-gradient(circle at 50% 50%, #f9f4ed 0%, #ebddc5 100%)' }}
        ref={stageRef}
      >
        <div
          className="graph-stage absolute top-0 left-0"
          style={{
            width: graph.width,
            height: graph.height,
            transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
            transformOrigin: '0 0',
          }}
        >
          <svg
            className="graph-svg pointer-events-none"
            viewBox={`0 0 ${graph.width} ${graph.height}`}
            width={graph.width}
            height={graph.height}
            style={{ position: 'absolute', top: 0, left: 0 }}
          >
            {graph.edges.map((edge, i) => {
              const from = graph.positions.get(edge.from);
              const to = graph.positions.get(edge.to);
              if (!from || !to) return null;
              const dx = Math.max(40, Math.abs(to.cx - from.cx) * 0.5);
              const path = `M ${from.cx} ${from.cy} C ${from.cx + dx} ${from.cy}, ${to.cx - dx} ${to.cy}, ${to.cx} ${to.cy}`;
              return (
                <path
                  key={i}
                  className={cn('graph-edge', edge.stale && 'stale', edge.crossProject && 'cross')}
                  d={path}
                />
              );
            })}
          </svg>

          <div className="graph-node-layer absolute top-0 left-0 w-full h-full">
            {graph.nodes.map(node => {
              const pos = graph.positions.get(node.id);
              if (!pos) return null;
              const accent = statusColor(node.status, node.stale);
              const badgeStatus = node.status === 'cancelled' ? 'skip' : node.status;
              const roleChip = node.role_id
                ? roles.find(r => r.id === node.role_id)?.name
                : null;
              return (
                <button
                  key={node.id}
                  type="button"
                  className={cn(
                    'graph-node',
                    'absolute flex flex-col cursor-pointer text-left font-[inherit] text-[inherit]',
                    node.primary ? 'primary' : 'external',
                    node.archived && 'archived',
                    node.stale && 'stale'
                  )}
                  style={{
                    left: pos.x, top: pos.y, width: NODE_W, height: NODE_H,
                    background: 'white',
                    border: '2px solid var(--node-accent, #645c50)',
                    borderLeft: '4px solid var(--node-accent, #645c50)',
                    borderRadius: 8,
                    padding: '8px 10px',
                    transition: 'box-shadow 0.15s',
                    ['--node-accent' as any]: accent,
                    ...(node.archived ? { opacity: 0.5 } : {}),
                    ...(node.primary ? {} : { opacity: 0.7, borderStyle: 'dashed' }),
                  }}
                  onClick={() => setModModalTaskId(node.id)}
                  data-tip={`${node.title} · #${node.id} · ${STATUS_LABEL[node.status] || node.status}`}
                >
                  <div className="gn-top flex justify-between items-center mb-1">
                    <span
                      className="gn-chip gn-task text-2xs px-[5px] py-px rounded-sm whitespace-nowrap font-semibold"
                      style={{ background: projectColor('').replace('hsl', ''), opacity: 0.15, color: '#645c50' }}
                    >
                      Task #{node.id}
                    </span>
                    <span className={cn('status-badge', `sb-${badgeStatus}`, 'text-2xs px-[5px] py-px rounded-sm whitespace-nowrap font-semibold')}>
                      {statusGlyph(node.status)} {STATUS_LABEL[node.status] || node.status}
                    </span>
                  </div>
                  <div className="gn-title text-sm font-semibold leading-[1.2] overflow-hidden text-ellipsis flex-1" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
                    {node.title}
                  </div>
                  <div className="gn-meta flex gap-1 mt-1 flex-wrap">
                    {roleChip && <span className="gn-chip gn-role text-2xs px-[5px] py-px rounded-sm whitespace-nowrap" style={{ background: '#e1eecc', color: '#56633f' }}>{roleChip}</span>}
                    {!roleChip && node.model && <span className="gn-chip gn-model text-2xs px-[5px] py-px rounded-sm whitespace-nowrap" style={{ background: '#e3ebf5', color: '#3f628f' }}>{node.model}</span>}
                    {node.stale && <span className="gn-chip gn-stale text-2xs px-[5px] py-px rounded-sm whitespace-nowrap" style={{ background: '#fff2eb', color: '#8c491a' }}>{'\u26A0'} Stale</span>}
                    {node.archived && <span className="gn-chip gn-archived text-2xs px-[5px] py-px rounded-sm whitespace-nowrap" style={{ background: '#eee7db', color: '#645c50' }}>Archived</span>}
                  </div>
                  <div className="gn-footer flex justify-between items-center mt-auto text-xs text-faint">
                    <span className="gn-counts flex gap-1.5">
                      <span title="Upstream">{'\u2191'} {node.dep_up_count}</span>
                      <span title="Downstream">{'\u2193'} {node.dep_down_count}</span>
                    </span>
                    <span>{node.primary ? 'Project' : 'Context'}</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
};

export default DependencyGraph;