import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";
import { BenchmarkPanel } from "./components/BenchmarkPanel";
import { ClusterPanel } from "./components/ClusterPanel";
import { Header } from "./components/Header";
import { JobLauncher } from "./components/JobLauncher";
import { JobView } from "./components/JobView";
import { PairDialog } from "./components/PairDialog";
import { JobsList, Timeline } from "./components/Sidebar";

export default function App() {
  const [pairing, setPairing] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [pinned, setPinned] = useState(false);
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.jobs(25) });

  // Follow the newest job unless the user picked an older one.
  const newest = jobs.data?.[0]?.id ?? null;
  useEffect(() => {
    if (!pinned && newest) setSelected(newest);
  }, [newest, pinned]);

  const select = (id: string) => {
    setSelected(id);
    setPinned(id !== newest);
  };

  return (
    <div className="min-h-full">
      <Header onAddLaptop={() => setPairing(true)} />
      <main className="mx-auto grid max-w-[1500px] gap-5 px-4 py-6 sm:px-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="min-w-0 space-y-5">
          <ClusterPanel onAddLaptop={() => setPairing(true)} />
          <JobLauncher onSubmitted={(id) => { setSelected(id); setPinned(false); }} />
          <JobView jobId={selected} />
          <BenchmarkPanel />
        </div>
        <aside className="min-w-0 space-y-5 xl:sticky xl:top-[76px] xl:self-start">
          <JobsList selected={selected} onSelect={select} />
          <Timeline onSelectJob={select} />
        </aside>
      </main>
      <footer className="mx-auto max-w-[1500px] px-6 pb-8 text-xs text-ink-500">
        Trusted laptops on one network · built-in workloads only · results verified by SHA-256
      </footer>
      <PairDialog open={pairing} onClose={() => setPairing(false)} />
    </div>
  );
}
