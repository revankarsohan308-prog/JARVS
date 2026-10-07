// J.A.R.V.S preload bridge — exposes only read-only runtime facts.
const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('jarvis', {
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  apiBase: 'http://127.0.0.1:8760/api/v1',
});