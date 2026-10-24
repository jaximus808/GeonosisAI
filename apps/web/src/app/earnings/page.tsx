import { EarningsView } from '@/components/payments/EarningsView';

// TDM-64: a contributor's earnings. Without ?contributor=N it shows the contributor bound
// to this browser's identity (signed up at /join); with it, the dashboard can open anyone's.
export default async function EarningsPage({
  searchParams,
}: {
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
}) {
  const raw = (await searchParams).contributor;
  const id = typeof raw === 'string' && /^\d{1,18}$/.test(raw) && raw !== '0' ? raw : undefined;
  return <EarningsView contributorParam={id} />;
}
