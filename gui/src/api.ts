export const BRIDGE = import.meta.env.DEV ? "" : "http://127.0.0.1:3001";
export const api = (path: string) => `${BRIDGE}${path}`;
export const artifact = (path: string) =>
  path.startsWith("http") ? path : api(path);
export const wsUrl = (path: string) =>
  import.meta.env.DEV
    ? `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`
    : `ws://127.0.0.1:3001${path}`;
