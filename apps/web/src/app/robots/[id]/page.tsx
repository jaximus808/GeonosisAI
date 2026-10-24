import { Tdm78Boundary } from '@/components/tdm78/Tdm78Boundary';
import { Tdm78RailCommands, Tdm78RobotConsole } from '@/components/tdm78/Tdm78RobotConsole';
import { Tdm78SoftEstop } from '@/components/tdm78/Tdm78SoftEstop';

// TDM-78: the robot page. The soft e-stop is outside every error boundary and needs only the id:
// fixed to the bottom of the screen on phones, pinned at the top of the right rail on desktop.
export default async function RobotPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const valid = /^\d{1,15}$/.test(id);
  return (
    <div className="pb-36 lg:grid lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-6 lg:pb-0">
      <div className="min-w-0">
        {valid ? (
          <Tdm78Boundary label="Robot page">
            <Tdm78RobotConsole id={id} />
          </Tdm78Boundary>
        ) : (
          <p className="text-sm text-zinc-600">&quot;{id}&quot; isn&apos;t a robot id.</p>
        )}
      </div>
      {valid && (
        <aside className="contents lg:block">
          <div className="fixed inset-x-0 bottom-0 z-40 p-2 lg:sticky lg:top-4 lg:z-30 lg:p-0">
            <Tdm78SoftEstop robotId={id} />
          </div>
          <div className="mt-4 hidden lg:block">
            <Tdm78Boundary label="Commands">
              <Tdm78RailCommands id={id} />
            </Tdm78Boundary>
          </div>
        </aside>
      )}
    </div>
  );
}
