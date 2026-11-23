import { KeyRound } from 'lucide-react';
import './providerKeyBanner.css';

export type ProviderKeyRefusal = 'provider_key_rejected' | 'provider_access_denied';

/** The server reports only a fixed code; the words live here. */
export function providerKeyRefusal(value: unknown): ProviderKeyRefusal | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const status = (value as { status?: unknown }).status;
  return status === 'provider_key_rejected' || status === 'provider_access_denied' ? status : undefined;
}

const TEXT: Record<ProviderKeyRefusal, { title: string; action: string }> = {
  provider_key_rejected: {
    title: 'YouTube rejected the API key',
    action: 'Replace the key from the installation folder:',
  },
  provider_access_denied: {
    title: 'YouTube refused access with the API key',
    action: "Enable YouTube Data API v3 for the key's project and allow this server in the key's restrictions, then run (the same key is fine):",
  },
};

export function ProviderKeyBanner({ refusal }: { refusal?: ProviderKeyRefusal }) {
  if (!refusal) return null;
  const text = TEXT[refusal];
  return <section className="provider-key-banner" role="alert" aria-label={text.title}>
    <KeyRound size={18} aria-hidden="true" />
    <div>
      <strong>{text.title}</strong>
      <p>Collection is stopped and no requests are sent to YouTube. Stored channels and videos stay readable.</p>
      <p>{text.action} <code>sudo python3 manage.py set-key</code></p>
      <p>Collection resumes by itself once YouTube accepts the key.</p>
    </div>
  </section>;
}
