import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { Bot, Check, Copy, Eye, EyeOff, KeyRound, Link2, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { ExplorerApiError, ExplorerNetworkError, isAbortError, requestJson } from './api';
import { copyText } from './clipboard';
import {
  AGENT_CLIENTS, KEY_CREATE_TEXT, LIST_NOT_READ, clientSettings, cursorInstallUrl, daysLeft, keyCreateOutcomeUnknown, keyDate, keyErrorMessage,
  maskedSecret, parseAgentConnections, parseAgentKeyList, parseCreatedAgentKey, signInSettings, validKeyDays, validKeyName,
  type AgentClient, type AgentConnection, type AgentKeyList, type CreatedAgentKey,
} from './agentsModel';
import './agents.css';

function message(error: unknown, fallback: string, maxKeys?: number): string {
  return error instanceof ExplorerApiError ? keyErrorMessage(error.code, error.message, maxKeys) : fallback;
}

/** Why a list was not read: the server's reason, or one "No connection…" for both lists. */
function readProblem(error: unknown, fallback: string): string {
  return error instanceof ExplorerNetworkError ? error.message : message(error, fallback);
}

/** A list read and parsed, or why not. */
function settled<T>(result: PromiseSettledResult<unknown>, parse: (value: unknown) => T): { value?: T; reason?: unknown } {
  if (result.status === 'rejected') return { reason: result.reason };
  try { return { value: parse(result.value) }; } catch (reason) { return { reason }; }
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState<boolean | null>(null);
  useEffect(() => {
    if (copied === null) return undefined;
    const timer = window.setTimeout(() => setCopied(null), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return <button type="button" className="agents-copy" onClick={async () => setCopied(await copyText(text))}>
    {copied ? <Check size={15} aria-hidden="true" /> : <Copy size={15} aria-hidden="true" />}
    <span>{copied === null ? label : copied ? 'Copied' : 'Copy failed: select and copy by hand'}</span>
  </button>;
}

function NewKey({ created, shown, onToggle, onDone }: {
  created: CreatedAgentKey; shown: boolean; onToggle: () => void; onDone: () => void;
}) {
  return <section className="agents-new-key" aria-labelledby="agents-new-key-heading">
    <div className="agents-section-heading"><h2 id="agents-new-key-heading">New key: {created.key.name}</h2></div>
    <p className="agents-warning">Shown only now. Paste it into your agent's settings or a private key file, never into a chat.
      If you lose it, create another key and revoke this one.</p>
    <div className="agents-secret">
      <code aria-label={shown ? 'Agent key' : 'Agent key, hidden'}>{shown ? created.secret : maskedSecret(created.secret)}</code>
      <button type="button" onClick={onToggle} aria-pressed={shown}>
        {shown ? <EyeOff size={15} aria-hidden="true" /> : <Eye size={15} aria-hidden="true" />}<span>{shown ? 'Hide' : 'Show'}</span>
      </button>
      <CopyButton text={created.secret} label="Copy key" />
    </div>
    <button type="button" className="agents-done" onClick={onDone}>Done, forget the key on this page</button>
  </section>;
}

type Mode = 'signin' | 'key';

function Connect({ list, created, shown, mode, onMode }: {
  list: AgentKeyList; created?: CreatedAgentKey; shown: boolean; mode: Mode; onMode: (mode: Mode) => void;
}) {
  const [client, setClient] = useState<AgentClient>('Codex');
  const signIn = signInSettings(client, list.mcpUrl);
  const keyed = clientSettings(client, list.mcpUrl, created?.secret);
  const useKey = mode === 'key' || signIn === null;
  const real = useKey || signIn === null ? keyed : signIn;
  const visible = useKey && created && !shown ? clientSettings(client, list.mcpUrl, maskedSecret(created.secret)) : real;
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(new URL(list.mcpUrl).hostname);
  const project = client === 'Codex' || client === 'Claude Code';
  const desktop = client === 'Claude Desktop';
  const quickSetup = desktop ? local : !useKey && (!project || local);
  const settings = <>
    <p className="agents-note">{real.where}</p>
    <pre className="agents-settings" aria-label={client + ' settings'}>{visible.text}</pre>
    <div className="agents-actions">
      <CopyButton text={real.text} label={useKey && created ? 'Copy with key' : 'Copy'} />
      {useKey && !created && <span className="agents-note">Create a key below to fill it in, or replace YOUR_KEY yourself.</span>}
    </div>
  </>;
  return <section className="agents-section" aria-labelledby="agents-connect-heading">
    <div className="agents-section-heading"><h2 id="agents-connect-heading">Connect an agent</h2><span>{list.mcpUrl}</span></div>
    <div className="agents-tabs" role="tablist" aria-label="Agent program">
      {AGENT_CLIENTS.map((name) => <button key={name} type="button" role="tab" aria-selected={name === client}
        className={name === client ? 'is-active' : undefined} onClick={() => setClient(name)}>{name}</button>)}
    </div>
    <div className="agents-modes" role="radiogroup" aria-label="How the program gets access">
      <button type="button" role="radio" aria-checked={!useKey} disabled={signIn === null}
        className={!useKey ? 'is-active' : undefined} onClick={() => onMode('signin')}>Sign in with Allow</button>
      <button type="button" role="radio" aria-checked={useKey}
        className={useKey ? 'is-active' : undefined} onClick={() => onMode('key')}>With a key</button>
    </div>
    {quickSetup && (client === 'Cursor' || client === 'VS Code') && <>
      <a className="agents-install" href={client === 'Cursor' ? cursorInstallUrl(list.mcpUrl)
        : 'vscode:mcp/install?' + encodeURIComponent(JSON.stringify({ name: 'leti-cook', type: 'http', url: list.mcpUrl }))}>
        Open in {client}</a>
      <p className="agents-note">Confirm the installation in {client}, then sign in and choose Allow read access.</p>
    </>}
    {quickSetup && desktop && <>
      <a className="agents-install" href="/agent-setup/claude-desktop" download="leti-cook.mcpb">Download Claude extension</a>
      <p className="agents-note">Open the download in Claude Desktop, choose Install and paste your Leti Cook agent key.
        If your browser only saves the file, choose Install Extension in Claude Desktop’s advanced extension settings.</p>
    </>}
    {quickSetup && project && <>
      <a className="agents-install" href={client === 'Codex' ? '/agent-setup/codex' : '/agent-setup/claude-code'}
        download>Download research project</a>
      <p className="agents-note">Extract the ZIP and open the Leti Cook folder in {client}. If its command line is installed,
        you can use the included Windows or macOS launcher. {client === 'Codex' ? 'Trust the project and authenticate leti-cook.' :
          'Approve the project server, then select leti-cook → Authenticate in /mcp.'} Choose Allow read access in your browser.</p>
    </>}
    {quickSetup ? <details className="agents-manual" key={client}><summary>Manual setup</summary>
      {client === 'Codex' && <>
        <div className="agents-quick-action"><CopyButton text={list.mcpUrl} label="Copy MCP address" /></div>
        <p className="agents-note">For all your projects, open Settings → MCP servers → Add server.
          Name it leti-cook, choose Streamable HTTP and paste the address. Save, select Restart, then Authenticate.</p>
      </>}
      {settings}</details> : settings}
    <p className="agents-note">This address works where this page opens: on this computer, or through the SSH tunnel you use
      for it. claude.ai, ChatGPT and Claude Desktop connectors run in the provider's cloud and cannot reach it.</p>
  </section>;
}

function Connections({ connections, failed, busy, confirming, onConfirm, onRevoke }: {
  /** Undefined until the list is read: an unread list is never shown as an empty one. */
  connections?: AgentConnection[]; failed: boolean; busy: boolean; confirming?: string;
  onConfirm: (id?: string) => void; onRevoke: (id: string) => void;
}) {
  return <section className="agents-section" aria-labelledby="agents-connections-heading">
    <div className="agents-section-heading"><h2 id="agents-connections-heading"><Link2 size={17} aria-hidden="true" />Connected programs</h2>
      <span>{connections ? `${connections.length} allowed` : '—'}</span></div>
    {failed && <p className="agents-note">{LIST_NOT_READ}</p>}
    {connections && !connections.length && <p className="agents-note">None yet. A program appears here after you click Allow for it.</p>}
    {connections && connections.length > 0 && <table className="agents-table">
      <thead><tr><th scope="col">Program</th><th scope="col">Answer went to</th><th scope="col">Allowed</th><th scope="col">Expires</th>
        <th scope="col"><span className="explorer-sr-only">Actions</span></th></tr></thead>
      <tbody>{connections.map((item) => <tr key={item.id}>
        <td><Bot size={14} aria-hidden="true" />{item.name}</td>
        <td>{item.redirect_host}</td>
        <td>{keyDate(item.created_at)}{item.used_at ? <span className="agents-note"> · used {keyDate(item.used_at)}</span> : null}</td>
        <td>{keyDate(item.expires_at)} <span className="agents-note">· {daysLeft(item.expires_at)} days left</span></td>
        <td>{confirming === 'c:' + item.id
          ? <span className="agents-confirm"><button type="button" className="is-danger" disabled={busy} onClick={() => onRevoke(item.id)}>Revoke now</button>
              <button type="button" disabled={busy} onClick={() => onConfirm(undefined)}>Keep</button></span>
          : <button type="button" disabled={busy} onClick={() => onConfirm('c:' + item.id)}><Trash2 size={14} aria-hidden="true" /><span>Revoke</span></button>}</td>
      </tr>)}</tbody>
    </table>}
    <p className="agents-note">The name is what the program called itself. Revoking ends its access at once; it must ask for
      Allow again.</p>
  </section>;
}

export function AgentsPage() {
  const [list, setList] = useState<AgentKeyList>();
  const [loading, setLoading] = useState(true);
  // The last action's outcome (create, revoke) and why a list was not read; one line, no phrase twice.
  const [notice, setNotice] = useState<string>();
  const [problems, setProblems] = useState<string[]>([]);
  const [keysFailed, setKeysFailed] = useState(false);
  const [name, setName] = useState('');
  const [days, setDays] = useState('30');
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<CreatedAgentKey>();
  const [shown, setShown] = useState(false);
  const [confirming, setConfirming] = useState<string>();
  const [connections, setConnections] = useState<AgentConnection[]>();
  const [mode, setMode] = useState<Mode>('signin');

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    try {
      // Two stores, read independently: a problem with one never hides the other.
      const [keys, allowed] = await Promise.allSettled([requestJson('/agent-keys', signal), requestJson('/agent-connections', signal)]);
      if (signal?.aborted) return;
      const found: string[] = [];
      const keyRead = settled(keys, parseAgentKeyList), connectionRead = settled(allowed, parseAgentConnections);
      if (keyRead.value) { setList(keyRead.value); setKeysFailed(false); }
      else if (!isAbortError(keyRead.reason)) { setKeysFailed(true); found.push(readProblem(keyRead.reason, 'Agent keys could not be read.')); }
      if (connectionRead.value) setConnections(connectionRead.value);
      else if (!isAbortError(connectionRead.reason)) {
        setConnections(undefined);
        found.push(readProblem(connectionRead.reason, 'Connected programs could not be read.'));
      }
      setProblems([...new Set(found)]);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const alert = [...new Set([notice, ...problems].filter((text): text is string => Boolean(text)))];
  const lifetime = Number(days);
  const ready = validKeyName(name) && validKeyDays(lifetime) && !busy;
  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    try {
      let answer: unknown;
      try {
        answer = await requestJson('/agent-keys', undefined, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.trim(), days: lifetime }),
        });
      } catch (reason) {
        // No answer: the key may exist. Only a read follows, never another create.
        // A refusal at the limit names the server's limit from the key list.
        if (!keyCreateOutcomeUnknown(reason)) { setNotice(message(reason, 'The key was not created.', list?.maxKeys)); return; }
        setNotice(KEY_CREATE_TEXT.unknown);
        await load();
        return;
      }
      let value: CreatedAgentKey;
      try { value = parseCreatedAgentKey(answer); } catch {
        setNotice(KEY_CREATE_TEXT.unreadable);
        await load();
        return;
      }
      setCreated(value);
      setShown(false);
      setMode('key');
      setName('');
      setNotice(undefined);
      await load();
    } finally {
      setBusy(false);
    }
  };
  const revoke = async (id: string, kind: 'keys' | 'connections' = 'keys') => {
    setBusy(true);
    try {
      await requestJson((kind === 'keys' ? '/agent-keys/' : '/agent-connections/') + encodeURIComponent(id), undefined, { method: 'DELETE' });
      if (created?.key.id === id) setCreated(undefined);
      setConfirming(undefined);
      setNotice(undefined);
      await load();
    } catch (reason) {
      setNotice(message(reason, kind === 'keys' ? 'The key was not revoked.' : 'The connection was not revoked.'));
    } finally {
      setBusy(false);
    }
  };

  return <main className="agents-workspace">
    <header className="agents-page-header">
      <div><p className="eyebrow">Operations</p><h1>Agents</h1>
        <p className="agents-page-description">Let an AI agent on your computer read this library through MCP.
          Read-only: an agent cannot collect from YouTube, change settings, or save, edit or delete anything.</p></div>
      <button type="button" className="agents-refresh" onClick={() => { setNotice(undefined); void load(); }} disabled={loading || busy}>
        <RefreshCw size={15} className={loading ? 'spin' : ''} aria-hidden="true" /><span>Refresh</span></button>
    </header>
    {alert.length > 0 && <p className="agents-error" role="alert">{alert.join(' ')}</p>}

    {created && <NewKey created={created} shown={shown} onToggle={() => setShown((value) => !value)}
      onDone={() => { setCreated(undefined); setShown(false); }} />}

    {list && <Connect list={list} created={created} shown={shown} mode={mode} onMode={setMode} />}

    <Connections connections={connections} failed={!loading && !connections} busy={busy} confirming={confirming} onConfirm={setConfirming}
      onRevoke={(id) => void revoke(id, 'connections')} />

    <section className="agents-section" aria-labelledby="agents-keys-heading">
      <div className="agents-section-heading"><h2 id="agents-keys-heading"><KeyRound size={17} aria-hidden="true" />Keys</h2>
        <span>{list && !keysFailed ? `${list.keys.length} of ${list.maxKeys}` : '—'}</span></div>
      <form className="agents-create" onSubmit={create}>
        <label><span>Name</span><input value={name} maxLength={80} onChange={(event) => setName(event.target.value)}
          placeholder="Claude Code on my laptop" /></label>
        <label><span>Lifetime, days</span><input type="number" min={1} max={365} value={days}
          onChange={(event) => setDays(event.target.value)} /></label>
        <button type="submit" disabled={!ready}><Plus size={15} aria-hidden="true" /><span>{busy ? 'Working…' : 'Create key'}</span></button>
      </form>
      {keysFailed && !loading && <p className="agents-note">{LIST_NOT_READ}</p>}
      {list && !keysFailed && !list.keys.length && <p className="agents-note">No keys yet. Each agent program should get its own key.</p>}
      {list && !keysFailed && list.keys.length > 0 && <table className="agents-table">
        <thead><tr><th scope="col">Name</th><th scope="col">Created</th><th scope="col">Expires</th><th scope="col"><span className="explorer-sr-only">Actions</span></th></tr></thead>
        <tbody>{list.keys.map((key) => <tr key={key.id}>
          <td><Bot size={14} aria-hidden="true" />{key.name}</td>
          <td>{keyDate(key.created_at)}</td>
          <td>{keyDate(key.expires_at)} <span className="agents-note">· {daysLeft(key.expires_at)} days left</span></td>
          <td>{confirming === key.id
            ? <span className="agents-confirm"><button type="button" className="is-danger" disabled={busy} onClick={() => void revoke(key.id)}>Revoke now</button>
                <button type="button" disabled={busy} onClick={() => setConfirming(undefined)}>Keep</button></span>
            : <button type="button" disabled={busy} onClick={() => setConfirming(key.id)}><Trash2 size={14} aria-hidden="true" /><span>Revoke</span></button>}</td>
        </tr>)}</tbody>
      </table>}
      <p className="agents-note">Revoking ends access at once, including a read in progress. Changing the owner password or
        access mode revokes every key.</p>
    </section>
  </main>;
}
