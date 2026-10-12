// SLNG Studio as a desktop app: an Electron window around `slng studio up`.
//
// The studio itself is unchanged: this starts it from the repository as a child (or uses one already
// running), shows its own loading screen (#/boot), and stops it with SIGINT on quit, so runners finish
// their calls and the containers stop, as ctrl+c does in a terminal. Electron is Chromium, the only
// browser the playback reports of a voice call are tested on.
//
// The microphone is allowed for the studio's own origin only, and only once macOS has said yes.
// Links to anywhere else open in the default browser. A run that ends posts a native notification.
const { app, BrowserWindow, Menu, Notification, session, shell, systemPreferences } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');

const PACKAGE = require('./package.json');
const ROOT = process.env.SLNG_STUDIO_ROOT || PACKAGE.studioRoot || path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.STUDIO_PORT || 7800);
const ORIGIN = `http://127.0.0.1:${PORT}`;
const ICON = path.join(__dirname, 'resources', 'icon.png');

/** The studio process: started here, or found already running (then left alone on quit). */
class Studio {
  constructor() {
    this.child = null;
    this.stopping = null;
  }

  /** Whether a studio answers at its origin. */
  static async answers() {
    try {
      return (await fetch(`${ORIGIN}/api/health`, { signal: AbortSignal.timeout(1000) })).ok;
    } catch {
      return false;
    }
  }

  /** Start `slng studio up` from the repository, unless a studio already answers on the port. */
  async start() {
    if (await Studio.answers()) return;
    // A login shell, so uv and docker are on PATH when the app is opened from the Finder or the Dock.
    const command = 'exec uv run --directory studio slng studio up';
    this.child =
      process.platform === 'win32'
        ? spawn('uv', ['run', '--directory', 'studio', 'slng', 'studio', 'up'], { cwd: ROOT, stdio: 'inherit' })
        : spawn(process.env.SHELL || '/bin/zsh', ['-lc', command], { cwd: ROOT, stdio: 'inherit' });
    this.child.on('exit', () => {
      this.child = null;
    });
  }

  /** Stop it as ctrl+c does, and wait until it has: runners drain their calls first. */
  stop() {
    if (!this.child) return Promise.resolve();
    this.stopping ??= new Promise((done) => {
      this.child.once('exit', done);
      this.child.kill('SIGINT');
    });
    return this.stopping;
  }
}

/** Runs that end while the app is open, each said once in a native notification that opens the run. */
class RunWatch {
  constructor(window) {
    this.window = window;
    this.live = new Set();
    this.timer = setInterval(() => void this.check(), 4000);
  }

  async check() {
    let runs;
    try {
      runs = await (await fetch(`${ORIGIN}/api/runs`)).json();
    } catch {
      return;
    }
    for (const run of runs) {
      if (run.outcome === null) this.live.add(run.id);
      else if (this.live.delete(run.id)) this.notify(run);
    }
  }

  notify(run) {
    if (!Notification.isSupported()) return;
    const note = new Notification({ title: `Run ended: ${run.outcome}`, body: `${run.agent} · ${run.channel} · ${run.turns} turns`, silent: true });
    note.on('click', () => {
      this.window.show();
      void this.window.loadURL(`${ORIGIN}/#/run/${encodeURIComponent(run.id)}`);
    });
    note.show();
  }

  stop() {
    clearInterval(this.timer);
  }
}

/** What the window may do: the studio's own page gets the microphone, every other site opens in the browser. */
class Guard {
  /** Whether a URL is the studio's own page. */
  static ours(url) {
    try {
      return new URL(url).origin === ORIGIN;
    } catch {
      return false;
    }
  }

  /** The microphone for the studio's own page only, after macOS consent; every other permission is refused. */
  static permissions() {
    session.defaultSession.setPermissionRequestHandler((_contents, permission, callback, details) => {
      const audioOnly = (details.mediaTypes ?? []).every((type) => type === 'audio');
      if (permission !== 'media' || !Guard.ours(details.requestingUrl) || !audioOnly) return callback(false);
      if (process.platform !== 'darwin') return callback(true);
      systemPreferences.askForMediaAccess('microphone').then(callback, () => callback(false));
    });
    session.defaultSession.setPermissionCheckHandler((_contents, permission, requestingOrigin) => permission === 'media' && requestingOrigin === ORIGIN);
  }

  /** New windows and navigation away from the studio go to the default browser instead. */
  static links(window) {
    window.webContents.setWindowOpenHandler(({ url }) => {
      if (!Guard.ours(url)) void shell.openExternal(url);
      return { action: 'deny' };
    });
    window.webContents.on('will-navigate', (event, url) => {
      if (!Guard.ours(url)) {
        event.preventDefault();
        void shell.openExternal(url);
      }
    });
  }
}

/** The app menu: the usual roles (Edit makes copy and paste work in the page), and Go for the three places. */
class AppMenu {
  static build(window) {
    const open = (hash) => () => void window.loadURL(`${ORIGIN}/${hash}`);
    return Menu.buildFromTemplate([
      ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
      { role: 'fileMenu' },
      { role: 'editMenu' },
      { role: 'viewMenu' },
      {
        label: 'Go',
        submenu: [
          { label: 'Agents', accelerator: 'CmdOrCtrl+1', click: open('#/agents') },
          { label: 'Runs', accelerator: 'CmdOrCtrl+2', click: open('#/runs') },
          { label: 'Metrics', accelerator: 'CmdOrCtrl+3', click: open('#/metrics') },
          { type: 'separator' },
          { label: 'Start-up', click: open('#/boot') },
          { label: 'Open in the Browser', click: () => void shell.openExternal(window.webContents.getURL()) },
        ],
      },
      { role: 'windowMenu' },
    ]);
  }
}

const studio = new Studio();
let watch = null;

app.whenReady().then(async () => {
  if (process.platform === 'darwin' && !app.isPackaged) app.dock?.setIcon(ICON);
  Guard.permissions();
  const window = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 390,
    title: 'SLNG Studio',
    backgroundColor: '#15120d',
    icon: ICON,
    webPreferences: { contextIsolation: true, sandbox: true },
  });
  Guard.links(window);
  Menu.setApplicationMenu(AppMenu.build(window));
  await studio.start();
  while (!(await Studio.answers())) await new Promise((wait) => setTimeout(wait, 500));
  await window.loadURL(`${ORIGIN}/#/boot`);
  watch = new RunWatch(window);
});

app.on('before-quit', (event) => {
  if (!studio.child) return;
  event.preventDefault();
  watch?.stop();
  for (const window of BrowserWindow.getAllWindows()) window.hide();
  void studio.stop().then(() => app.quit());
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  BrowserWindow.getAllWindows()[0]?.show();
});
