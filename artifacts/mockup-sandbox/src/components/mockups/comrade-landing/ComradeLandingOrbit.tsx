import { useMemo, useState } from "react";
import {
  ArrowUpRight,
  Check,
  ChevronRight,
  Circle,
  Command,
  GitBranch,
  MessageSquare,
  Play,
  Search,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-react";

const display = { fontFamily: "'Bricolage Grotesque', 'Trebuchet MS', sans-serif" };
const mono = { fontFamily: "'Space Mono', 'SFMono-Regular', monospace" };

type Room = {
  id: string;
  name: string;
  eyebrow: string;
  copy: string;
  accent: string;
  initials: string;
  state: string;
  signal: string;
};

const rooms: Room[] = [
  {
    id: "release",
    name: "Release room",
    eyebrow: "Shipping / 04",
    copy: "The smallest useful picture of what is ready, what is blocked, and what changed since yesterday.",
    accent: "#f47762",
    initials: "RL",
    state: "2 decisions waiting",
    signal: "steady",
  },
  {
    id: "research",
    name: "Research room",
    eyebrow: "Discovery / 07",
    copy: "Questions, evidence, and the threads that deserve a human answer before the roadmap moves.",
    accent: "#69b7a5",
    initials: "RS",
    state: "5 sources cited",
    signal: "growing",
  },
  {
    id: "rituals",
    name: "Team rituals",
    eyebrow: "Culture / 02",
    copy: "A gentle record of how this group works together, without turning the work into performance.",
    accent: "#f0b866",
    initials: "TR",
    state: "next sync in 18m",
    signal: "open",
  },
];

function StatusDot({ color }: { color: string }) {
  return <span className="h-2 w-2 rounded-full" style={{ background: color }} />;
}

export function ComradeLandingOrbit() {
  const [activeRoom, setActiveRoom] = useState("release");
  const [joined, setJoined] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const room = useMemo(() => rooms.find((item) => item.id === activeRoom) ?? rooms[0], [activeRoom]);

  return (
    <main className="min-h-[100dvh] overflow-hidden bg-[#f5f1e9] text-[#242638]">
      <div className="mx-auto max-w-[1440px] px-5 py-5 sm:px-8 lg:px-12">
        <nav className="flex items-center justify-between border-b border-[#d9d2c5] pb-5">
          <button className="flex items-center gap-3" onClick={() => setActiveRoom("release")}>
            <span className="grid h-9 w-9 place-items-center rounded-[13px] bg-[#242638] text-[#ffd1b4]">
              <Sparkles size={17} />
            </span>
            <span className="text-xl font-bold tracking-[-.06em]" style={display}>comrade<span className="text-[#f47762]">.</span></span>
          </button>
          <div className="hidden items-center gap-8 text-sm text-[#777574] md:flex">
            <span className="text-[#242638]">Your rooms</span>
            <button onClick={() => setPaletteOpen(true)} className="flex items-center gap-2 transition-colors hover:text-[#f47762]">
              <Command size={14} /> Jump anywhere <span className="rounded border border-[#d0c7ba] px-1.5 py-0.5 text-[10px]" style={mono}>⌘K</span>
            </button>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={() => setJoined(!joined)} className="hidden rounded-full bg-[#242638] px-4 py-2.5 text-xs font-bold text-[#fcfbf8] transition-transform hover:-translate-y-0.5 sm:block">
              {joined ? "You’re in" : "Start a room"} <ArrowUpRight className="ml-1 inline" size={14} />
            </button>
            <button aria-label="Open menu" onClick={() => setMenuOpen(!menuOpen)} className="rounded-full border border-[#d9d2c5] p-2.5 md:hidden">
              {menuOpen ? <X size={18} /> : <Command size={18} />}
            </button>
          </div>
        </nav>

        {menuOpen && (
          <div className="mt-3 flex flex-col gap-3 rounded-2xl border border-[#d9d2c5] bg-[#fbf8f2] p-4 text-sm shadow-sm md:hidden">
            <button onClick={() => setPaletteOpen(true)} className="flex items-center gap-2 text-left"><Search size={15} /> Jump anywhere</button>
            <button onClick={() => { setJoined(true); setMenuOpen(false); }} className="flex items-center gap-2 text-left"><ArrowUpRight size={15} /> Start a room</button>
          </div>
        )}

        <section className="grid gap-12 pb-20 pt-16 lg:grid-cols-[.82fr_1.18fr] lg:items-center lg:gap-20 lg:pb-28 lg:pt-24">
          <div>
            <div className="mb-6 flex items-center gap-3 text-[10px] font-bold uppercase tracking-[.22em] text-[#f47762]" style={mono}>
              <span className="h-2 w-2 rounded-full bg-[#f47762]" /> A team room with a memory
            </div>
            <h1 className="max-w-xl text-6xl font-bold leading-[.88] tracking-[-.075em] sm:text-7xl lg:text-[6.7rem]" style={display}>
              Make the<br /><span className="text-[#f47762]">room</span> legible.
            </h1>
            <p className="mt-8 max-w-md text-lg leading-relaxed text-[#6e6c69]">
              Comrade turns scattered work into a living room your team can actually think inside. Choose a signal. Follow the thread. Decide together.
            </p>
            <div className="mt-9 flex flex-wrap items-center gap-4">
              <button onClick={() => setJoined(true)} className="rounded-full bg-[#f47762] px-6 py-3.5 text-sm font-bold text-[#242638] transition-transform hover:-translate-y-1">
                {joined ? <><Check className="mr-2 inline" size={16} /> Room reserved</> : <>Open your first room <ArrowUpRight className="ml-2 inline" size={16} /></>}
              </button>
              <button onClick={() => setPaletteOpen(true)} className="flex items-center gap-2 text-sm font-bold text-[#6e6c69] transition-colors hover:text-[#f47762]">
                <span className="grid h-8 w-8 place-items-center rounded-full border border-[#d5cdbf]"><Play size={12} fill="currentColor" /></span> See the 60-second version
              </button>
            </div>
          </div>

          <div className="relative min-h-[440px] rounded-[30px] bg-[#242638] p-5 text-[#fcfbf8] shadow-[12px_16px_0_#e6ddd1] sm:p-8">
            <div className="absolute inset-0 opacity-20" style={{ backgroundImage: "radial-gradient(#ffd1b4 1px, transparent 1px)", backgroundSize: "22px 22px", maskImage: "linear-gradient(135deg, black, transparent 72%)" }} />
            <div className="relative">
              <div className="flex items-center justify-between border-b border-[#4d4e60] pb-5">
                <div><p className="text-[10px] uppercase tracking-[.2em] text-[#aaa9b5]" style={mono}>Live team picture</p><p className="mt-2 text-xl font-bold tracking-[-.04em]" style={display}>What needs us now</p></div>
                <div className="flex items-center gap-2 rounded-full border border-[#4d4e60] px-3 py-1.5 text-[10px] text-[#c7c4cb]" style={mono}><StatusDot color="#69b7a5" /> synced 2m ago</div>
              </div>
              <div className="mt-8 grid grid-cols-[1fr_auto] gap-5">
                <div>
                  <p className="text-4xl font-bold leading-none tracking-[-.06em]" style={display}>3 active<br /><span className="text-[#ffd1b4]">rooms.</span></p>
                  <p className="mt-4 max-w-[220px] text-sm leading-relaxed text-[#b8b6c0]">No dashboards to maintain. Just the context that lets a good conversation start faster.</p>
                </div>
                <div className="relative h-36 w-36 rounded-full border border-[#5e5f6d]">
                  <div className="absolute inset-5 rounded-full border border-[#777887]" />
                  <div className="absolute left-1/2 top-1/2 h-9 w-9 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[#f47762]" />
                  <span className="absolute -right-2 top-7 h-3 w-3 rounded-full bg-[#69b7a5]" /><span className="absolute bottom-1 left-6 h-3 w-3 rounded-full bg-[#f0b866]" />
                </div>
              </div>
              <div className="mt-9 space-y-2">
                {rooms.map((item) => (
                  <button key={item.id} onClick={() => setActiveRoom(item.id)} className={`flex w-full items-center gap-3 rounded-2xl border p-3 text-left transition-transform hover:translate-x-1 ${activeRoom === item.id ? "border-[#777887] bg-[#303147]" : "border-transparent"}`}>
                    <span className="grid h-9 w-9 place-items-center rounded-xl text-[10px] font-bold text-[#242638]" style={{ background: item.accent }}>{item.initials}</span>
                    <span className="min-w-0 flex-1"><span className="block text-sm font-bold">{item.name}</span><span className="block truncate text-xs text-[#aaa9b5]">{item.state}</span></span>
                    <ChevronRight size={15} className="text-[#aaa9b5]" />
                  </button>
                ))}
              </div>
            </div>
          </div>
        </section>

        <section className="border-t border-[#d9d2c5] py-16 lg:py-24">
          <div className="grid gap-12 lg:grid-cols-[.6fr_1.4fr]">
            <div><p className="text-[10px] font-bold uppercase tracking-[.22em] text-[#f47762]" style={mono}>Rooms, not feeds</p><h2 className="mt-5 max-w-xs text-4xl font-bold leading-[.92] tracking-[-.065em] lg:text-6xl" style={display}>Follow the signal.</h2></div>
            <div>
              <div className="mb-7 flex flex-wrap gap-2">
                {rooms.map((item) => <button key={item.id} onClick={() => setActiveRoom(item.id)} className={`rounded-full border px-4 py-2 text-xs font-bold transition-colors ${activeRoom === item.id ? "border-[#242638] bg-[#242638] text-[#fcfbf8]" : "border-[#d3ccbf] text-[#777574] hover:border-[#f47762]"}`}>{item.name}</button>)}
              </div>
              <div className="grid gap-8 rounded-[26px] border border-[#d9d2c5] bg-[#fbf8f2] p-6 sm:grid-cols-[1fr_auto] sm:p-9">
                <div><p className="text-[10px] uppercase tracking-[.2em] text-[#f47762]" style={mono}>{room.eyebrow}</p><h3 className="mt-4 text-3xl font-bold tracking-[-.06em]" style={display}>{room.name}</h3><p className="mt-4 max-w-lg text-base leading-relaxed text-[#6e6c69]">{room.copy}</p><button onClick={() => setPaletteOpen(true)} className="mt-7 text-sm font-bold underline decoration-[#f47762] decoration-2 underline-offset-4">Enter this room <ArrowUpRight className="ml-1 inline" size={14} /></button></div>
                <div className="flex min-w-[150px] flex-col justify-between gap-7 border-t border-[#ded7ca] pt-5 sm:border-l sm:border-t-0 sm:pl-7 sm:pt-0"><div><p className="text-3xl font-bold tracking-[-.06em]" style={display}>01</p><p className="mt-1 text-xs text-[#777574]">current focus</p></div><div><p className="flex items-center gap-2 text-xs font-bold"><StatusDot color={room.accent} /> {room.signal}</p><p className="mt-2 text-xs leading-relaxed text-[#777574]">{room.state}</p></div></div>
              </div>
            </div>
          </div>
        </section>

        <section className="grid gap-5 pb-16 sm:grid-cols-3 lg:pb-24">
          {[
            [MessageSquare, "Conversation becomes context", "The right thread is waiting where the decision lives."],
            [GitBranch, "Work keeps its receipts", "Every answer points back to the source that earned it."],
            [ShieldCheck, "Agency stays human", "Comrade can surface the next move. Only people make it."],
          ].map(([Icon, title, copy]) => (
            <div key={title as string} className="rounded-[24px] border border-[#d9d2c5] bg-[#efe9df] p-6"><Icon size={20} className="text-[#f47762]" /><h3 className="mt-12 text-xl font-bold tracking-[-.04em]" style={display}>{title as string}</h3><p className="mt-3 text-sm leading-relaxed text-[#777574]">{copy as string}</p></div>
          ))}
        </section>

        <footer className="flex flex-col justify-between gap-4 border-t border-[#d9d2c5] py-7 text-[10px] uppercase tracking-[.17em] text-[#8c8882] sm:flex-row" style={mono}><span>comrade / make the room legible</span><span className="flex items-center gap-2"><Users size={13} /> built for teams that lead themselves</span><span>© 2025</span></footer>
      </div>

      {paletteOpen && (
        <div className="fixed inset-0 z-50 grid place-items-start bg-[#242638]/45 p-5 pt-[16vh]" onClick={() => setPaletteOpen(false)}>
          <div role="dialog" aria-modal="true" onClick={(event) => event.stopPropagation()} className="w-full max-w-xl overflow-hidden rounded-[24px] bg-[#fbf8f2] shadow-2xl">
            <div className="flex items-center gap-3 border-b border-[#ded7ca] px-5 py-4"><Search size={18} className="text-[#8c8882]" /><input autoFocus placeholder="Search rooms, decisions, people..." className="flex-1 bg-transparent text-sm outline-none placeholder:text-[#aaa49d]" /><button onClick={() => setPaletteOpen(false)}><X size={17} /></button></div>
            <div className="p-3"><p className="px-3 py-2 text-[10px] uppercase tracking-[.2em] text-[#8c8882]" style={mono}>Jump to a room</p>{rooms.map((item) => <button key={item.id} onClick={() => { setActiveRoom(item.id); setPaletteOpen(false); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-3 text-left hover:bg-[#efe9df]"><Circle size={11} fill={item.accent} color={item.accent} /><span className="flex-1 text-sm font-bold">{item.name}</span><span className="text-xs text-[#8c8882]">{item.eyebrow}</span></button>)}</div>
          </div>
        </div>
      )}
    </main>
  );
}