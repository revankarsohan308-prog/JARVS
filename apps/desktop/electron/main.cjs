// J.A.R.V.S desktop shell (Electron main process).
// Loads the React command center and keeps the window sandboxed.
const { app, BrowserWindow } = require('electron');
const path = require('path');

const isDev = process.argv.includes('--dev');
const DEV_URL = process.env.JARVIS_DEV_URL || 'http://127.0.0.1:5173';

function createWindow() {
  const win = new BrowserWindow({
    width: 1320,
    height: 840,
    minWidth: 1000,
    minHeight: 640,
    backgroundColor: '#05070f',
    title: 'J.A.R.V.S',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  if (isDev) {
    win.loadURL(DEV_URL);
  } else {
    // Production: the backend serves the built React UI at /jarvis/, so API
    // calls are same-origin (no CORS, HttpOnly cookie just works).
    win.loadURL('http://127.0.0.1:8760/jarvis/');
  }
}

app.whenReady().then(() => {
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});