export interface ProviderBucket {
  slot: number | null;
  key: string;
  label: string;
  color: string;
}

export const PROVIDER_BUCKETS: ProviderBucket[] = [
  { slot: null, key: 'unassigned', label: 'Unassigned', color: '#8b949e' },
  { slot: 1, key: 'claude', label: 'Claude Pro', color: '#FF9500' },
  { slot: 2, key: 'mistral', label: 'Mistral Pro', color: '#58a6ff' },
  { slot: 3, key: 'payg', label: 'PAYG', color: '#00a67e' },
  { slot: 4, key: 'scaleway', label: 'EU Scaleway', color: '#d2a8ff' },
  { slot: 5, key: 'ollama', label: 'Ollama Cloud', color: '#0b8f6a' },
];

export const SLOT_LABELS: Record<string, string> = {
  '0': 'Unassigned',
  '1': 'Claude',
  '2': 'Mistral',
  '3': 'PAYG',
  '4': 'Scaleway',
  '5': 'Ollama',
};

export const PROVIDER_BY_SLOT: Record<number, ProviderBucket> = PROVIDER_BUCKETS
  .reduce((acc, b) => {
    if (b.slot !== null) acc[b.slot] = b;
    return acc;
  }, {} as Record<number, ProviderBucket>);

export function allowedSlotsForModel(modelId: string): (number | null)[] {
  const m = modelId || '';
  if (m.startsWith('mistral-ocr')) return [null, 2, 3, 4]; // EU-operated, API-only — also Vault-friendly (slot 4)
  if (m.startsWith('scw-')) return [null, 4];
  if (m.startsWith('oll-')) return [null, 5];
  if (m.startsWith('claude-')) return [null, 1, 3];
  if (m.startsWith('mistral-') || m.startsWith('open-mistral') || m.startsWith('codestral-') || m.startsWith('devstral-')) return [null, 2, 3];
  return [null, 3];
}