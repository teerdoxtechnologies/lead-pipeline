import { useQuery } from '@tanstack/react-query';
import { api } from './api';

/**
 * Public site links (Vercel). The backend stores two static trees side by side:
 *   websites/preview/{slug}/  → generated website previews
 *   websites/audit/{slug}     → published audit reports
 * so the path prefix depends on where the page is stored. The domain itself
 * comes from the backend runtime config, with a compiled fallback.
 */
export const SITE_BASE_FALLBACK = 'https://teerdoxlabs.vercel.app';

export function auditPath(slug: string): string {
  return `/audit/${slug}`;
}

export function previewPath(slug: string): string {
  return `/preview/${slug}/`;
}

export function useSiteBase(): string {
  const q = useQuery({
    queryKey: ['site-base'],
    queryFn: () => api.runtimeConfig().then((r) => String((r as Record<string, unknown>).base_url ?? '')),
    staleTime: 5 * 60_000,
    retry: 1,
  });
  const base = (q.data ?? '').trim().replace(/\/+$/, '');
  return base || SITE_BASE_FALLBACK;
}

export function useSiteUrls() {
  const base = useSiteBase();
  return {
    base,
    auditUrl: (slug: string) => `${base}${auditPath(slug)}`,
    previewUrl: (slug: string) => `${base}${previewPath(slug)}`,
  };
}
