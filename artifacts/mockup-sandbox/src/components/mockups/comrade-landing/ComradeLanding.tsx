import { useState, type ReactNode } from "react";
import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleCheck,
  FileText,
  GitBranch,
  LockKeyhole,
  Menu,
  MessageSquare,
  Network,
  Play,
  Quote,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-react";

const mono = { fontFamily: "'Space Mono', 'SFMono-Regular', monospace" };
const display = { fontFamily: "'Bricolage Grotesque', 'Trebuchet MS', sans-serif" };

function DotGrid() {
  return (
    <div
      aria-hidden="true"
      className="pointer-events-none absolute inset-0 opacity-[0.14]"
      style={{
        backgroundImage:
          "radial-gradient(circle, rgba(215,255,68,.9) 1px, transparent 1px)",
        backgroundSize: "24px 24px",
        maskImage: "linear-gradient(90deg, black, transparent 70%)",
      }}
    />
  );
}

function SectionLabel({ children, light = false }: { children: ReactNode; light?: boolean }) {
  return (
    <div
      className={`mb-5 flex items-center gap-3 text-[10px] font-bold uppercase tracking-[0.22em] ${
        light ? "text-[#d7ff44]" : "text-[#ef5b36]"
      }`}
      style={mono}
    >
      <span className={`h-2 w-2 rounded-full ${light ? "bg-[#d7ff44]" : "bg-[#ef5b36]"}`} />
      {children}
    </div>
  );
}

export function ComradeLanding() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [approved, setApproved] = useState(false);
  const [showDemo, setShowDemo] = useState(false);

  const scrollTo = (id: string) => {
    setMenuOpen(false);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth" });
  };

  return (
    <main className="min-h-[100dvh] overflow-hidden bg-[#f3f0e8] text-[#142b38]">
      <section className="relative bg-[#142b38] text-[#f3f0e8]">
        <DotGrid />
        <nav className="relative z-10 mx-auto flex max-w-7xl items-center justify-between px-6 py-6 lg:px-10">
          <button onClick={() => scrollTo("top")} className="group flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-[11px] bg-[#d7ff44] text-[#142b38] transition-transform group-hover:rotate-[-8deg]">
              <Network size={19} strokeWidth={2.5} />
            </span>
            <span className="text-xl font-bold tracking-[-0.05em]" style={display}>comrade</span>
          </button>
          <div className="hidden items-center gap-8 text-sm text-[#b6c5c8] md:flex">
            <button onClick={() => scrollTo("how")} className="transition-colors hover:text-[#d7ff44]">How it works</button>
            <button onClick={() => scrollTo("trust")} className="transition-colors hover:text-[#d7ff44]">Trust, by design</button>
            <button onClick={() => scrollTo("teams")} className="transition-colors hover:text-[#d7ff44]">For teams</button>
          </div>
          <button
            onClick={() => scrollTo("join")}
            className="hidden rounded-full bg-[#f3f0e8] px-5 py-2.5 text-sm font-bold text-[#142b38] transition-all hover:bg-[#d7ff44] md:block"
          >
            Bring Comrade in <ArrowRight className="ml-1 inline-block" size={15} />
          </button>
          <button aria-label="Toggle menu" onClick={() => setMenuOpen(!menuOpen)} className="md:hidden">
            {menuOpen ? <X /> : <Menu />}
          </button>
        </nav>
        {menuOpen && (
          <div className="relative z-20 mx-6 mb-2 rounded-2xl border border-[#41606a] bg-[#1c3a48] p-4 md:hidden">
            {["how", "trust", "teams", "join"].map((item) => (
              <button key={item} onClick={() => scrollTo(item)} className="block w-full border-b border-[#41606a] py-3 text-left text-sm capitalize last:border-0">
                {item === "join" ? "Bring Comrade in" : item.replace("-", " ")}
              </button>
            ))}
          </div>
        )}

        <div id="top" className="relative z-10 mx-auto grid max-w-7xl gap-14 px-6 pb-24 pt-16 lg:grid-cols-[1.03fr_.97fr] lg:px-10 lg:pb-32 lg:pt-24">
          <div className="flex flex-col justify-center">
            <div className="mb-7 inline-flex w-fit items-center gap-2 rounded-full border border-[#41606a] px-3 py-1.5 text-[10px] uppercase tracking-[0.18em] text-[#b6c5c8]" style={mono}>
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#d7ff44]" /> A shared team room for autonomous teams
            </div>
            <h1 className="max-w-3xl text-[clamp(3.8rem,8vw,7.7rem)] font-bold leading-[0.86] tracking-[-0.075em]" style={display}>
              Confusion out.<br /><span className="text-[#d7ff44]">Coherence in.</span>
            </h1>
            <p className="mt-8 max-w-xl text-lg leading-relaxed text-[#b6c5c8]">
              Comrade is the quiet teammate that keeps an engineering team’s shared picture intact — while the work gets louder, faster, and more distributed.
            </p>
            <div className="mt-9 flex flex-wrap items-center gap-3">
              <button onClick={() => scrollTo("join")} className="rounded-full bg-[#d7ff44] px-6 py-3.5 text-sm font-bold text-[#142b38] transition-transform hover:-translate-y-1">
                See Comrade in action <ArrowRight className="ml-2 inline" size={16} />
              </button>
              <button onClick={() => setShowDemo(!showDemo)} className="group flex items-center gap-2 px-4 py-3.5 text-sm font-semibold text-[#f3f0e8]">
                <span className="flex h-8 w-8 items-center justify-center rounded-full border border-[#6b858a] transition-colors group-hover:border-[#d7ff44] group-hover:text-[#d7ff44]"><Play size={13} fill="currentColor" /></span>
                Watch the 60 sec idea
              </button>
            </div>
            {showDemo && <div className="mt-4 max-w-md rounded-xl border border-[#41606a] bg-[#1c3a48] p-4 text-sm text-[#d4e0df]">“Planning did not get cheaper.” Comrade turns the fragments already in your team into a picture you can act on. No theatrics. Just the next true thing.</div>}
          </div>

          <div className="relative min-h-[490px] lg:min-h-[560px]">
            <div className="absolute right-0 top-5 h-[82%] w-[88%] rounded-[32px] bg-[#e4dfd1] p-3 shadow-2xl shadow-[#071920]/30 sm:w-[82%]">
              <div className="h-full overflow-hidden rounded-[23px] border border-[#c8c3b5] bg-[#f8f6f0] text-[#142b38]">
                <div className="flex items-center justify-between border-b border-[#d8d4c9] px-5 py-4">
                  <div className="flex items-center gap-2"><span className="h-2.5 w-2.5 rounded-full bg-[#d7ff44]" /><span className="text-xs font-bold">northstar / team room</span></div>
                  <span className="text-[9px] uppercase tracking-widest text-[#789096]" style={mono}>live context</span>
                </div>
                <div className="p-5">
                  <div className="mb-5 flex gap-3">
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-[#142b38] text-[#d7ff44]"><Sparkles size={15} /></span>
                    <div><p className="text-xs font-bold">Comrade <span className="ml-1 font-normal text-[#789096]">just now</span></p><p className="mt-1 text-[13px] leading-relaxed">I found three threads converging on the same decision.</p></div>
                  </div>
                  <div className="ml-11 rounded-xl border border-[#d8d4c9] bg-[#efede6] p-4">
                    <div className="mb-3 flex items-center justify-between"><span className="text-[10px] font-bold uppercase tracking-wider text-[#ef5b36]" style={mono}>proposed wiki update</span><span className="rounded-full bg-[#d7ff44] px-2 py-1 text-[9px] font-bold">3 citations</span></div>
                    <p className="text-sm font-bold">Use queue-based retries for webhook delivery.</p>
                    <div className="mt-3 space-y-2 text-[11px] text-[#536d73]">
                      <p><span className="mr-2 text-[#ef5b36]">↳</span>#backend · “a queue gives us…”</p>
                      <p><span className="mr-2 text-[#ef5b36]">↳</span>PR 184 · retry design notes</p>
                      <p><span className="mr-2 text-[#ef5b36]">↳</span>Jules · “shipping this Thursday”</p>
                    </div>
                    <button onClick={() => setApproved(!approved)} className={`mt-4 w-full rounded-lg py-2.5 text-xs font-bold transition-colors ${approved ? "bg-[#142b38] text-[#d7ff44]" : "bg-[#ef5b36] text-[#fff8ee] hover:bg-[#d84d2c]"}`}>
                      {approved ? <><Check className="mr-1 inline" size={14} /> Approved by you</> : "Review & approve update"}
                    </button>
                  </div>
                  <div className="mt-5 flex items-center gap-2 text-[10px] text-[#789096]" style={mono}><LockKeyhole size={12} /> nothing leaves this room without consent</div>
                </div>
              </div>
            </div>
            <div className="absolute bottom-2 left-0 w-[55%] rounded-2xl border border-[#41606a] bg-[#1c3a48] p-4 shadow-xl sm:w-[48%]">
              <div className="mb-3 flex items-center gap-2 text-[#d7ff44]"><ShieldCheck size={17} /><span className="text-[10px] font-bold uppercase tracking-wider" style={mono}>consent checkpoint</span></div>
              <p className="text-sm leading-relaxed text-[#dbe6e3]">Comrade proposes.<br /><strong className="text-white">A human decides.</strong></p>
            </div>
          </div>
        </div>
        <div className="relative z-10 border-t border-[#36535d]">
          <div className="mx-auto flex max-w-7xl flex-wrap gap-x-10 gap-y-3 px-6 py-5 text-[10px] uppercase tracking-[0.17em] text-[#8ea8aa] lg:px-10" style={mono}>
            <span className="flex items-center gap-2"><GitBranch size={13} /> repository-aware</span><span className="flex items-center gap-2"><FileText size={13} /> cited memory</span><span className="flex items-center gap-2"><Users size={13} /> human-led action</span>
          </div>
        </div>
      </section>

      <section id="how" className="mx-auto max-w-7xl px-6 py-24 lg:px-10 lg:py-36">
        <SectionLabel>The coordination layer</SectionLabel>
        <div className="grid gap-10 lg:grid-cols-[.9fr_1.1fr]">
          <h2 className="max-w-lg text-5xl font-bold leading-[0.95] tracking-[-0.06em] lg:text-7xl" style={display}>Your team has the answers.<br /><span className="text-[#ef5b36]">They’re just everywhere.</span></h2>
          <div className="flex flex-col justify-end"><p className="max-w-xl text-lg leading-relaxed text-[#526b72]">Chat moves fast. Repositories move quietly. Decisions dissolve between them. Comrade reads the room, connects the evidence, and gives the team back a shared point of view.</p><div className="mt-8 h-px w-full bg-[#d1cec3]" /><div className="mt-5 flex items-center justify-between text-xs text-[#789096]" style={mono}><span>context in</span><span className="text-[#ef5b36]">coherence out →</span></div></div>
        </div>
        <div className="mt-16 grid gap-4 md:grid-cols-3">
          {[
            { n: "01", icon: MessageSquare, title: "Reads the room", body: "Chat, documents, deadlines, and repository activity — held together without another dashboard to maintain." },
            { n: "02", icon: FileText, title: "Remembers with receipts", body: "The shared wiki is compiled from versioned facts, with citations that take you back to the source." },
            { n: "03", icon: CircleCheck, title: "Closes the loop", body: "Surfaces the ownerless decision, the unspoken risk, and the next move before they become expensive." },
          ].map(({ n, icon: Icon, title, body }) => (
            <article key={n} className="group border-t-2 border-[#142b38] pt-5 transition-transform hover:-translate-y-1">
              <div className="flex items-start justify-between"><span className="text-xs text-[#ef5b36]" style={mono}>{n}</span><Icon size={21} strokeWidth={1.7} className="text-[#ef5b36] transition-transform group-hover:rotate-12" /></div>
              <h3 className="mt-16 text-2xl font-bold tracking-[-0.04em]" style={display}>{title}</h3><p className="mt-3 leading-relaxed text-[#60777b]">{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="trust" className="bg-[#e4dfd1] px-6 py-24 lg:px-10 lg:py-32">
        <div className="mx-auto max-w-7xl">
          <SectionLabel>Trust, not vibes</SectionLabel>
          <div className="grid gap-14 lg:grid-cols-[.8fr_1.2fr]">
            <div><h2 className="text-5xl font-bold leading-[.94] tracking-[-.06em] lg:text-7xl" style={display}>Useful enough<br /><span className="text-[#ef5b36]">to trust.</span></h2><p className="mt-7 max-w-sm leading-relaxed text-[#60777b]">The guardrails are not a settings page. They are the product.</p></div>
            <div className="divide-y divide-[#c7c2b5]">
              {[
                ["Human consent", "The agent never performs a group-visible action it chose itself. It proposes. A human approves. A separate role executes."],
                ["Cited memory", "Every fact in Comrade’s shared wiki carries its source. No mysterious summaries, no “trust me” layer between a decision and its evidence."],
                ["Identity, server-side", "The model never supplies identity. Team and requester are bound server-side and enforced through team-scoped roles."],
              ].map(([title, body], i) => <div key={title} className="flex gap-6 py-7 first:pt-0"><span className="mt-1 text-sm text-[#ef5b36]" style={mono}>0{i + 1}</span><div><h3 className="text-xl font-bold tracking-[-.03em]" style={display}>{title}</h3><p className="mt-2 max-w-lg leading-relaxed text-[#60777b]">{body}</p></div></div>)}
            </div>
          </div>
        </div>
      </section>

      <section id="teams" className="bg-[#ef5b36] px-6 py-24 text-[#fff8ee] lg:px-10 lg:py-32">
        <div className="mx-auto max-w-7xl">
          <div className="grid gap-12 lg:grid-cols-[1.1fr_.9fr] lg:items-end"><div><SectionLabel light>Built for the way teams actually work</SectionLabel><h2 className="max-w-3xl text-5xl font-bold leading-[.9] tracking-[-.065em] lg:text-8xl" style={display}>Less status theatre.<br />More shared reality.</h2></div><p className="max-w-sm text-lg leading-relaxed text-[#ffe3d5]">For small teams without a manager. For sub-teams inside big companies. For students shipping the thing together.</p></div>
          <div className="mt-20 grid gap-10 border-t border-[#f58d71] pt-7 sm:grid-cols-3">
            <div><p className="text-4xl font-bold tracking-[-.06em]" style={display}>19</p><p className="mt-2 text-sm text-[#ffe3d5]">native tools for team state, wiki, chat, docs, and repos</p></div>
            <div><p className="text-4xl font-bold tracking-[-.06em]" style={display}>1</p><p className="mt-2 text-sm text-[#ffe3d5]">shared picture, compiled from the work already happening</p></div>
            <div><p className="text-4xl font-bold tracking-[-.06em]" style={display}>0</p><p className="mt-2 text-sm text-[#ffe3d5]">actions taken behind your team’s back</p></div>
          </div>
        </div>
      </section>

      <section id="join" className="relative bg-[#142b38] px-6 py-24 text-[#f3f0e8] lg:px-10 lg:py-36">
        <DotGrid />
        <div className="relative z-10 mx-auto max-w-7xl">
          <div className="max-w-3xl"><SectionLabel light>Make the room clearer</SectionLabel><h2 className="text-5xl font-bold leading-[.92] tracking-[-.065em] lg:text-8xl" style={display}>Give your team<br /><span className="text-[#d7ff44]">a Comrade.</span></h2><p className="mt-8 max-w-lg text-lg leading-relaxed text-[#b6c5c8]">Bring your repository, your conversations, and your unfinished decisions. Start with a team room that remembers what matters.</p></div>
          <div className="mt-10 flex flex-wrap gap-3"><button onClick={() => setApproved(!approved)} className="rounded-full bg-[#d7ff44] px-7 py-4 text-sm font-bold text-[#142b38] transition-transform hover:-translate-y-1">{approved ? "You’re on the list" : "Join the early team"} <ArrowRight className="ml-2 inline" size={16} /></button><button onClick={() => scrollTo("trust")} className="rounded-full border border-[#5a747c] px-7 py-4 text-sm font-bold transition-colors hover:border-[#d7ff44] hover:text-[#d7ff44]">Read the principles <ChevronDown className="ml-2 inline" size={15} /></button></div>
          <div className="mt-28 flex flex-col justify-between gap-8 border-t border-[#36535d] pt-6 text-[10px] uppercase tracking-[.16em] text-[#8ea8aa] sm:flex-row" style={mono}><span>Comrade / coherence at speed</span><span>Built for teams that lead themselves</span><span>© 2025</span></div>
        </div>
      </section>
    </main>
  );
}