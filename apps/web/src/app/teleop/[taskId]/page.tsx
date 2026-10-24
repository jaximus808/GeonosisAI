import { Placeholder } from '@/components/Section';

export default async function TeleopPage({ params }: { params: Promise<{ taskId: string }> }) {
  const { taskId } = await params;
  return (
    <>
      <h1 className="mb-6 text-2xl font-semibold tracking-tight">Task {taskId}</h1>
      <Placeholder owner="E08 phone teleop">Camera feeds and clutch control.</Placeholder>
    </>
  );
}
