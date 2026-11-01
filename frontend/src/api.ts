import type { Snapshot } from './types';

function headers(token: string): HeadersInit {
  return { Authorization: `Bearer ${token}` };
}

export async function overview(token: string): Promise<Snapshot> {
  const response = await fetch('/api/overview', { headers: headers(token), cache: 'no-store' });
  if (!response.ok) throw new Error(response.status === 401 ? 'Access key not accepted' : 'Dashboard unavailable');
  return response.json() as Promise<Snapshot>;
}

export async function saveSettings(token: string, section: string, payload: unknown): Promise<string> {
  const response = await fetch(`/api/settings/${section}`, {
    method: 'PUT',
    headers: { ...headers(token), 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const result = await response.json() as { detail?: string; restart_workers?: boolean };
  if (!response.ok) throw new Error(result.detail || 'Could not save settings');
  return result.restart_workers ? 'Saved. Restart the affected background worker to apply it.' : 'Saved.';
}

export async function streamSnapshots(
  token: string,
  onSnapshot: (value: Snapshot) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch('/api/stream', { headers: headers(token), signal, cache: 'no-store' });
  if (!response.ok || !response.body) throw new Error('Live updates unavailable');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const event = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const data = event.split('\n').find((line) => line.startsWith('data: '));
        if (event.includes('event: snapshot') && data) {
          try { onSnapshot(JSON.parse(data.slice(6)) as Snapshot); } catch { /* one bad event is ignored */ }
        }
        boundary = buffer.indexOf('\n\n');
      }
    }
  } finally {
    reader.releaseLock();
  }
}
