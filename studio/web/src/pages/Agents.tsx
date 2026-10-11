import { lazy, Suspense, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Agent, ConfigEdit, EditorConfig, EditorStep, Problem } from '../api/types';
import { useApi } from '../api/useApi';
import { ClosingEditor } from '../closing/ClosingEditor';
import { FocusView } from '../components/FocusView';
import { MovesTable } from '../components/MovesTable';
import { Placeholder } from '../components/Placeholder';
import { Shell } from '../components/Shell';
import { TryPanel } from '../live/TryPanel';
import { useRun } from '../live/useRun';
import { useKey } from '../keys';
import { AGENT_TABS, href, go, useUnsaved, type Route } from '../router';
import { FactsTab } from './Facts';
import { MetricsBody } from './Metrics';
import { RunsTable } from './Runs';

const MapView = lazy(() => import('../components/MapView').then((m) => ({ default: m.MapView })));

/**
 * One agent: its config on the left, a real run on the right. Change a prompt, a closing line or an
 * action step's routes; save, and the studio checks the change on a copy first (the loader, then each
 * closing line against its receipt) and writes only what passed. Tabs open the same agent's facts, graph,
 * runs, metrics and whole manifest. Nothing here is shared with another agent's page. The draft lives across
 * the tabs; leaving the agent with it unsaved is asked about first, and Cmd/Ctrl+S saves it.
 */
export function AgentsPage({ route }: { route: Extract<Route, { page: 'agents' }> }) {
  const agents = useApi(api.agents, 'agents');
  const list = agents.state === 'ok' ? agents.data : [];
  const id = route.agent ?? list.find((a) => a.status === 'compiled')?.id ?? list[0]?.id;
  const agent = list.find((a) => a.id === id);
  const tab = route.tab ?? 'build';
  const [reload, setReload] = useState(0);
  const config = useApi(() => (id ? api.config(id) : Promise.reject(new Error('no agent'))), `${id}-${reload}`, true);
  const graph = useApi(() => (id ? api.agent(id) : Promise.reject(new Error('no agent'))), `graph-${id}-${reload}`, true);
  const [draft, setDraft] = useState<ConfigEdit>({});
  const [result, setResult] = useState<{ problems: Problem[]; message: string; ok: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const view = useRun(route.run);
  const dirty = Boolean(Object.keys(draft.files ?? {}).length || Object.keys(draft.closing ?? {}).length || Object.keys(draft.actions ?? {}).length || draft.manifest != null);
  const data = config.state === 'ok' ? config.data : undefined;

  const setRun = (run: string | undefined) => go({ page: 'agents', agent: id, tab, step: route.step, run });
  useUnsaved(dirty ? (next) => next.page !== 'agents' || (next.agent ?? id) !== id : null);

  const save = async (check = false) => {
    if (!id || !data) return;
    setSaving(true);
    try {
      const edit = { ...draft, base: data.version };
      const done = check ? await api.check(id, edit) : await api.save(id, edit);
      const saved = 'saved' in done && done.saved;
      setResult({
        ok: done.ok,
        problems: done.problems,
        message: saved ? `Saved as config ${done.version}, edited from ${data.version}. The next run uses it.` : done.ok ? `The checks pass. Saving makes config ${done.version}.` : 'Not saved: fix what is listed first.',
      });
      if (saved) {
        setDraft({});
        setReload((n) => n + 1);
      }
    } catch (failure) {
      setResult({ ok: false, problems: [], message: failure instanceof ApiError ? failure.message : String(failure) });
    } finally {
      setSaving(false);
    }
  };

  useKey('s', tab === 'facts' ? null : () => dirty && !saving && void save());

  return (
    <Shell
      route={route}
      agent={id}
      switcher
      who={view.status === 'live' ? (view.thinking ? 'engine' : null) : undefined}
      children={
        data && <span className="mono dim bar-note" style={{ fontSize: 12 }}>config {data.version} · {data.path}</span>
      }
      actions={
        tab === 'facts' ? undefined : <>
          {dirty && <span className="tag tag-y">unsaved</span>}
          <button type="button" className="btn btn-q" disabled={!dirty || saving} onClick={() => { setDraft({}); setResult(null); }}>Discard</button>
          <button type="button" className="btn btn-q" disabled={!dirty || saving} onClick={() => void save(true)}>Check</button>
          <button type="button" className="btn btn-y" disabled={!dirty || saving} onClick={() => void save()}>{saving ? 'Checking…' : 'Save'}</button>
        </>
      }
      body={
        <div style={{ flex: '1 1 auto', display: 'flex', flexDirection: 'column', minWidth: 0 }}>
          <nav className="tabs" aria-label="Agent">
            {AGENT_TABS.map((t) => (
              <a key={t.tab} href={href({ page: 'agents', agent: id, tab: t.tab, run: route.run })} aria-current={tab === t.tab ? 'page' : undefined}>{t.label}</a>
            ))}
          </nav>
          {result && (
            <div style={{ padding: '12px 24px', borderBottom: '1px solid var(--rule-soft)', display: 'flex', flexDirection: 'column', gap: 6 }}>
              <div className={result.ok ? 'good' : 'problem'}>{result.message}</div>
              {result.problems.map((p, i) => (
                <div key={i} className={p.level === 'warning' ? 'warn' : 'problem'}>{p.where}: {p.what}</div>
              ))}
            </div>
          )}
          {agents.state === 'error' && <p className="error-box" style={{ margin: 24 }}>{agents.message}</p>}
          {agent?.status === 'error' && <div className="error-box" style={{ margin: 24 }}>{agent.id} does not compile: {agent.error}</div>}
          {config.state === 'error' && <p className="error-box" style={{ margin: 24 }}>{config.message}</p>}
          {!data && config.state === 'loading' && <Placeholder what="the config" />}
          {data && id && tab === 'build' && (
            <div className="cols">
              <Steps config={data} selected={route.step} now={view.status === 'live' ? view.step : ''} agent={id} run={route.run} />
              <main className="col-main">
                <StepEditor config={data} step={data.steps.find((s) => s.id === route.step) ?? data.steps[0]} draft={draft} setDraft={setDraft} agent={id} />
              </main>
              <TryPanel agent={id} version={data.version} run={route.run} onRun={setRun} view={view} />
            </div>
          )}
          {data && id && tab === 'operations' && <Operations config={data} draft={draft} setDraft={setDraft} agent={id} focus={route.step} />}
          {id && tab === 'facts' && <FactsTab agent={id} picked={route.step} onSaved={() => setReload((n) => n + 1)} />}
          {id && tab === 'graph' && <GraphTab agent={graph.state === 'ok' ? graph.data : undefined} now={view.status === 'live' ? view.step : ''} step={route.step} />}
          {id && tab === 'runs' && (
            <div style={{ padding: 16 }}>
              <RunsTable agent={id} />
            </div>
          )}
          {id && tab === 'metrics' && <MetricsBody agent={id} fixed />}
          {data && id && tab === 'config' && <FullConfig config={data} draft={draft} setDraft={setDraft} />}
        </div>
      }
    />
  );
}

function Steps({ config, selected, now, agent, run }: { config: EditorConfig; selected?: string; now: string; agent: string; run?: string }) {
  const current = selected ?? config.steps[0]?.id;
  return (
    <nav className="col-steps" aria-label="Steps">
      <div className="side-head">
        <span className="lbl">Steps</span>
        <span style={{ flex: '1 1 auto' }} />
        <span className="mono dim" style={{ fontSize: 11 }}>{config.steps.length}</span>
      </div>
      {config.steps.map((s) => (
        <a key={s.id} className="side-link" href={href({ page: 'agents', agent, tab: 'build', step: s.id, run })}>
          <span className="side-item" aria-pressed={s.id === current} style={{ flexDirection: 'row', alignItems: 'center' }}>
            <span className="mono" style={{ fontSize: 13 }}>{s.id}</span>
            <span style={{ flex: '1 1 auto' }} />
            {now === s.id && <span className="mono pulse" style={{ fontSize: 10, color: 'var(--yellow)' }}>● now</span>}
            <span className="mono dim" style={{ fontSize: 10 }}>{s.kind === 'conversation' ? '' : s.kind}</span>
          </span>
        </a>
      ))}
    </nav>
  );
}

function fileOf(config: EditorConfig, draft: ConfigEdit, path: string): string {
  return draft.files?.[path] ?? config.files[path] ?? '';
}

function StepEditor({ config, step, draft, setDraft, agent }: { config: EditorConfig; step?: EditorStep; draft: ConfigEdit; setDraft: (d: ConfigEdit) => void; agent: string }) {
  if (!step) return <p className="empty">No steps.</p>;
  const ops = new Map(config.operations.map((o) => [o.name, o]));
  if (step.kind === 'action') {
    const op = ops.get(step.operation);
    const routes = { then: step.then, on_error: step.on_error, on_unknown: step.on_unknown, returns: step.returns, ...draft.actions?.[step.id] };
    const others = config.steps.map((s) => s.id);
    const setRoute = (key: 'then' | 'on_error' | 'on_unknown' | 'returns', value: string | boolean) =>
      setDraft({ ...draft, actions: { ...draft.actions, [step.id]: { ...draft.actions?.[step.id], [key]: value } } });
    return (
      <>
        <header>
          <div className="lbl">action step · no prompt, no model</div>
          <h1 className="mono" style={{ margin: '6px 0 0', fontSize: 28, fontWeight: 500 }}>{step.id}</h1>
        </header>
        <p style={{ margin: 0, color: 'var(--soft)', fontSize: 14, lineHeight: 1.5 }}>
          Runs <span className="mono">{step.operation}</span> with its arguments from the facts, says its closing line filled from the receipt, and moves on.
          A refusal says nothing unless a line by result names it; an unknown outcome says the locale's unsettled line.
        </p>
        {op && <ClosingEditor op={op} draft={draft} setDraft={setDraft} agent={agent} />}
        <div className="panel">
          <div className="panel-h"><span className="lbl">Where it goes next</span></div>
          <div className="panel-b" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 12 }}>
            {(['then', 'on_error', 'on_unknown'] as const).map((key) => (
              <div key={key} className="field">
                <label htmlFor={`route-${key}`}>{key === 'then' ? 'then (it worked)' : key === 'on_error' ? 'on_error (refused)' : 'on_unknown (no answer)'}</label>
                <select id={`route-${key}`} className="select" value={String(routes[key] ?? '')} onChange={(e) => setRoute(key, e.target.value)}>
                  <option value="">—</option>
                  {others.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </div>
            ))}
            <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: 14 }}>
              <input type="checkbox" checked={Boolean(routes.returns)} onChange={(e) => setRoute('returns', e.target.checked)} />
              returns to the step that handed the caller in
            </label>
          </div>
        </div>
      </>
    );
  }
  if (step.kind === 'end') {
    return (
      <header>
        <div className="lbl">end step</div>
        <h1 className="mono" style={{ margin: '6px 0 0', fontSize: 28, fontWeight: 500 }}>{step.id}</h1>
        <p className="hint">An end step says its line and closes the call. Edit it on the Full config tab.</p>
      </header>
    );
  }
  const prompt = step.prompt;
  return (
    <>
      <header>
        <div className="lbl">conversation step · agent {step.agent}</div>
        <h1 className="mono" style={{ margin: '6px 0 0', fontSize: 28, fontWeight: 500 }}>{step.id}</h1>
      </header>
      {prompt && (
        <div className="field">
          <label htmlFor="prompt">Prompt · {prompt}</label>
          <textarea
            id="prompt"
            className="textarea"
            rows={16}
            value={fileOf(config, draft, prompt)}
            onChange={(e) => setDraft({ ...draft, files: { ...draft.files, [prompt]: e.target.value } })}
          />
          <span className="hint">Prompts are compiled against the agent's facts when it loads: a placeholder that names no fact is caught on save.</span>
        </div>
      )}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 24 }}>
        <div className="field">
          <span className="lbl">Facts it may fill in</span>
          <div className="chips">{step.fields.length ? step.fields.map((f) => <span key={f} className="chip">{f}</span>) : <span className="hint">none</span>}</div>
        </div>
      </div>
      {step.operations.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <span className="lbl">Operations it may carry out, each read back first</span>
          {step.operations.map((name) => {
            const op = ops.get(name);
            return op ? <ClosingEditor key={name} op={op} draft={draft} setDraft={setDraft} agent={agent} /> : null;
          })}
        </div>
      )}
      <span className="hint">Exits, consults and context are edited on the Full config tab, checked by the loader the same way.</span>
    </>
  );
}

/** Every operation's closing lines. `focus` is an operation another page linked to: scrolled to and marked. */
function Operations({ config, draft, setDraft, agent, focus }: { config: EditorConfig; draft: ConfigEdit; setDraft: (d: ConfigEdit) => void; agent: string; focus?: string }) {
  const actionSteps = config.steps.filter((s) => s.kind === 'action');
  useEffect(() => {
    if (focus) document.getElementById(`op-${focus}`)?.scrollIntoView({ block: 'start' });
  }, [focus]);
  return (
    <main className="col-main" style={{ maxWidth: 980 }}>
      <div>
        <h2 style={{ margin: 0, fontSize: 24 }}>Every operation's closing lines</h2>
        <p style={{ color: 'var(--soft)', fontSize: 14, lineHeight: 1.5, maxWidth: 760 }}>
          Code says these, never the model: the closing line right after an operation runs, filled from its receipt, and the goodbye line when the caller leaves.
          A closing line can depend on the result (by result): one line when the customer is found, another when they are new, another when the service says no.
          Core does not check a line's fields when it loads; the studio checks each against the fields of its own result before it saves.
          {actionSteps.length > 0 && <> Action steps here: {actionSteps.map((s) => s.id).join(', ')}.</>}
        </p>
      </div>
      {config.operations.map((op) => (
        <section key={op.name} id={`op-${op.name}`} className={`op-anchor${focus === op.name ? ' op-focus' : ''}`} aria-label={op.name}>
          <ClosingEditor op={op} draft={draft} setDraft={setDraft} agent={agent} />
        </section>
      ))}
      {config.problems.length > 0 && config.problems.map((p, i) => <div key={i} className="problem">{p.where}: {p.what}</div>)}
    </main>
  );
}

function FullConfig({ config, draft, setDraft }: { config: EditorConfig; draft: ConfigEdit; setDraft: (d: ConfigEdit) => void }) {
  return (
    <main className="col-main">
      <div className="field">
        <label htmlFor="manifest">{config.path} · the whole manifest, checked by the loader before it is saved</label>
        <textarea id="manifest" className="textarea" rows={36} value={draft.manifest ?? config.manifest} onChange={(e) => setDraft({ ...draft, manifest: e.target.value })} spellCheck={false} />
        <span className="hint">Saving this replaces agent.yaml as typed. Closing-line and action edits on the other tabs are made line by line instead, keeping its comments.</span>
      </div>
    </main>
  );
}

function GraphTab({ agent, now, step }: { agent?: Agent; now: string; step?: string }) {
  const [view, setView] = useState<'focus' | 'map' | 'moves'>('focus');
  const graph = agent?.graph ?? undefined;
  const focus = useMemo(() => (step && graph?.nodes.some((n) => n.id === step) ? step : now || graph?.entry), [step, now, graph]);
  if (!agent) return <Placeholder what="the graph" />;
  if (!graph || !focus) return <p className="error-box" style={{ margin: 24 }}>{agent.error || 'No graph.'}</p>;
  const pick = (to: string) => {
    if (to !== '$end') go({ page: 'agents', agent: agent.id, tab: 'graph', step: to });
  };
  return (
    <main className="main">
      <div className="toolbar">
        <div className="segs" role="tablist" aria-label="Graph view">
          {(['focus', 'map', 'moves'] as const).map((v) => (
            <button key={v} type="button" role="tab" aria-selected={view === v} className={`seg ${view === v ? 'seg-on' : ''}`} onClick={() => setView(v)}>{v}</button>
          ))}
        </div>
        <span className="mono dim" style={{ fontSize: 12 }}>moves from the engine's own graph{now ? ` · the live run is at ${now}` : ''}</span>
      </div>
      <div className={`stage ${view === 'focus' ? 'dots' : ''}`}>
        {view === 'focus' && <FocusView graph={graph} step={focus} path={[]} status={now === focus ? 'live run here' : undefined} onPick={pick} />}
        {view === 'map' && (
          <Suspense fallback={<Placeholder what="the map" kind="rows" />}>
            <MapView graph={graph} focus={now || focus} onPick={pick} />
          </Suspense>
        )}
        {view === 'moves' && (
          <div style={{ width: '100%' }}>
            <MovesTable graph={graph} focus={focus} onPick={pick} />
          </div>
        )}
      </div>
    </main>
  );
}
