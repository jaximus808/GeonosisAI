import { Placeholder } from '@/components/Section';

export default async function ReportPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Report for ticket {id}</h1>
      <Placeholder owner="E11 report page">Data bought, why, cost, success before and after.</Placeholder>
    </>
  );
}
