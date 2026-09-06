import { useState } from 'react';
import {
  Inbox, Star, Send, Archive, Trash2, PenSquare, Search, ShieldCheck,
  Paperclip, ChevronDown, Reply, Forward, MoreHorizontal, CheckCheck,
  Clock, Lock, Filter, ArchiveX, Tag, FileText, ArrowLeft, BellOff
} from 'lucide-react';
import { motion, AnimatePresence } from 'framer-motion';

const folders = [
  { id: 'inbox', label: 'Inbox', icon: Inbox, count: 6 },
  { id: 'starred', label: 'Starred', icon: Star, count: 2 },
  { id: 'sent', label: 'Sent', icon: Send, count: null },
  { id: 'archive', label: 'Archive', icon: Archive, count: null },
  { id: 'trash', label: 'Trash', icon: Trash2, count: null },
];

const labels = [
  { name: 'Clients', color: '#2F5D44' },
  { name: 'Legal', color: '#8C5A2B' },
  { name: 'Finance', color: '#34506B' },
  { name: 'Personal', color: '#7A4A52' },
];

const messages = [
  {
    id: 1,
    sender: 'Eleanor Whitfield',
    org: 'Whitfield & Marsh LLP',
    initials: 'EW',
    avatarBg: '#2F5D44',
    verified: true,
    subject: 'Series B term sheet — final redlines attached',
    preview: 'Marcus, the counterparty accepted our liquidation preference language. Two outstanding points remain on the board composition clause…',
    time: '9:42 AM',
    unread: true,
    starred: true,
    label: 'Legal',
    labelColor: '#8C5A2B',
    attachments: 2,
    encrypted: true,
    body: [
      `Marcus,`,
      `The counterparty accepted our liquidation preference language without further negotiation — a meaningful win given where we started in March. Two outstanding points remain on the board composition clause, both of which I expect to resolve before Thursday's call.`,
      `First: they're requesting one independent seat be mutually agreed rather than founder-appointed. My recommendation is to accept, conditional on a 24-month sunset. Second: the observer rights for Halcyon Capital. I've drafted alternative language that limits information rights to quarterly materials only.`,
      `Redlined documents attached. I'd suggest we review together before anything goes back to their counsel.`,
    ],
    signoff: 'Eleanor Whitfield\nSenior Partner, Whitfield & Marsh LLP',
    files: [
      { name: 'TermSheet_SeriesB_v7_redline.docx', size: '482 KB' },
      { name: 'BoardComposition_AltLanguage.pdf', size: '118 KB' },
    ],
  },
  {
    id: 2,
    sender: 'Priya Raghunathan',
    org: 'Meridian Trust — Private Banking',
    initials: 'PR',
    avatarBg: '#34506B',
    verified: true,
    subject: 'Q3 portfolio review · proposed rebalancing',
    preview: 'Ahead of our meeting on the 14th, I\'ve prepared a summary of the quarter. Net of fees, the core portfolio returned 4.7%…',
    time: '8:15 AM',
    unread: true,
    starred: false,
    label: 'Finance',
    labelColor: '#34506B',
    attachments: 1,
    encrypted: true,
    body: [
      `Dear Marcus,`,
      `Ahead of our meeting on the 14th, I've prepared a summary of the quarter. Net of fees, the core portfolio returned 4.7%, outperforming the blended benchmark by 110 basis points, driven primarily by the overweight in industrial REITs we initiated in May.`,
      `I am proposing a modest rebalancing: trimming the technology sleeve by 3% and reallocating toward short-duration treasuries given the current yield environment. Full rationale is in the attached memorandum.`,
      `Please let me know if Thursday at 2:00 PM still works for you. I've held the private dining room at the Fifth Avenue office, as usual.`,
    ],
    signoff: 'Priya Raghunathan, CFA\nDirector, Meridian Trust Private Banking',
    files: [{ name: 'Q3_Portfolio_Review_Hale.pdf', size: '1.2 MB' }],
  },
  {
    id: 3,
    sender: 'Tomás Aguilar',
    org: 'Aguilar Architecture Studio',
    initials: 'TA',
    avatarBg: '#7A4A52',
    verified: false,
    subject: 'Amagansett residence — revised elevations',
    preview: 'We\'ve incorporated your notes on the western façade. The cedar screen now extends past the kitchen volume, which solves the afternoon glare…',
    time: 'Yesterday',
    unread: true,
    starred: false,
    label: 'Personal',
    labelColor: '#7A4A52',
    attachments: 4,
    encrypted: false,
    body: [
      `Hi Marcus,`,
      `We've incorporated your notes on the western façade. The cedar screen now extends past the kitchen volume, which solves the afternoon glare problem without compromising the ocean sightline from the main bedroom.`,
      `The structural engineer signed off on the cantilevered terrace yesterday. We're still waiting on the village review board, but our expediter expects approval by the end of the month.`,
      `Renderings and updated drawings attached — happy to walk through them whenever suits.`,
    ],
    signoff: 'Tomás Aguilar\nPrincipal, Aguilar Architecture Studio',
    files: [
      { name: 'Amagansett_West_Elevation_R4.pdf', size: '8.4 MB' },
      { name: 'Terrace_Structural_Approval.pdf', size: '236 KB' },
    ],
  },
  {
    id: 4,
    sender: 'Naomi Okafor',
    org: 'Chief of Staff',
    initials: 'NO',
    avatarBg: '#5B5147',
    verified: true,
    subject: 'Board dinner — seating and final agenda',
    preview: 'Confirmed for Tuesday at Le Coucou, private room. Seating chart attached — I placed Daniel next to the Halcyon partners per your note…',
    time: 'Yesterday',
    unread: false,
    starred: true,
    label: 'Clients',
    labelColor: '#2F5D44',
    attachments: 1,
    encrypted: false,
    body: [
      `Marcus,`,
      `Confirmed for Tuesday at Le Coucou, private room, 7:30 PM. Seating chart attached — I placed Daniel next to the Halcyon partners per your note, and kept Eleanor across from you in case the term sheet discussion continues over dinner.`,
      `Final agenda: Q3 results (15 min), Series B status (20 min), then open discussion. I've asked the kitchen to hold service during the financial review.`,
      `Car is booked for 7:00 from the office.`,
    ],
    signoff: 'Naomi',
    files: [{ name: 'BoardDinner_Seating_Oct.pdf', size: '94 KB' }],
  },
  {
    id: 5,
    sender: 'Dr. Sarah Lindqvist',
    org: 'Karolinska Advisory Board',
    initials: 'SL',
    avatarBg: '#3E5E5A',
    verified: true,
    subject: 'Advisory board renewal — 2025 term',
    preview: 'It has been a privilege having you on the foundation\'s advisory board these past three years. The trustees have asked me to extend…',
    time: 'Mon',
    unread: false,
    starred: false,
    label: 'Personal',
    labelColor: '#7A4A52',
    attachments: 0,
    encrypted: false,
    body: [
      `Dear Marcus,`,
      `It has been a privilege having you on the foundation's advisory board these past three years. The trustees have asked me to extend a formal invitation to renew your appointment for the 2025–2027 term.`,
      `The commitment remains the same: four meetings annually, two in Stockholm and two remote. We would, of course, be delighted if you continued chairing the grants committee.`,
      `No urgency — a response by the end of November would be perfect.`,
    ],
    signoff: 'Dr. Sarah Lindqvist\nExecutive Director, Karolinska Advisory Board',
    files: [],
  },
  {
    id: 6,
    sender: 'James Okonkwo',
    org: 'Halcyon Capital',
    initials: 'JO',
    avatarBg: '#6B4E2E',
    verified: true,
    subject: 'Re: Diligence follow-ups — customer references',
    preview: 'Thanks for the introductions. We spoke with all three reference customers this week. Feedback was uniformly strong, particularly around…',
    time: 'Mon',
    unread: false,
    starred: false,
    label: 'Clients',
    labelColor: '#2F5D44',
    attachments: 0,
    encrypted: true,
    body: [
      `Marcus,`,
      `Thanks for the introductions. We spoke with all three reference customers this week. Feedback was uniformly strong, particularly around implementation speed and the quality of your customer success team — Vantage called the onboarding "the best they've experienced from any vendor."`,
      `That closes out our commercial diligence. Legal is the last open workstream, and I understand Eleanor and our counsel are converging on the remaining points.`,
      `Looking forward to Tuesday's dinner.`,
    ],
    signoff: 'James Okonkwo\nPartner, Halcyon Capital',
    files: [],
  },
];

export default function App() {
  const [activeFolder, setActiveFolder] = useState('inbox');
  const [selectedId, setSelectedId] = useState(1);
  const [starred, setStarred] = useState(() => new Set(messages.filter(m => m.starred).map(m => m.id)));
  const [readIds, setReadIds] = useState(() => new Set(messages.filter(m => !m.unread).map(m => m.id)));
  const [query, setQuery] = useState('');
  const [filterUnread, setFilterUnread] = useState(false);

  const selected = messages.find(m => m.id === selectedId);

  const toggleStar = (id, e) => {
    e?.stopPropagation();
    setStarred(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const openMessage = (id) => {
    setSelectedId(id);
    setReadIds(prev => new Set(prev).add(id));
  };

  const visible = messages.filter(m => {
    if (activeFolder === 'starred' && !starred.has(m.id)) return false;
    if (filterUnread && readIds.has(m.id)) return false;
    if (query && !(m.sender + m.subject + m.preview).toLowerCase().includes(query.toLowerCase())) return false;
    return true;
  });

  const unreadCount = messages.filter(m => !readIds.has(m.id)).length;

  return (
    <div className="h-screen w-full flex flex-col bg-[#F4F1EA] text-[#1C1A16] antialiased overflow-hidden" style={{ fontFamily: "'Inter', sans-serif" }}>
      <link rel="preconnect" href="https://fonts.googleapis.com" />
      <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter:wght@400;450;500;550;600&display=swap" rel="stylesheet" />
      <style dangerouslySetInnerHTML={{ __html: `
        ::selection { background: #2F5D44; color: #F4F1EA; }
        .serif { font-family: 'Fraunces', serif; }
        .msg-list::-webkit-scrollbar, .reading::-webkit-scrollbar { width: 8px; }
        .msg-list::-webkit-scrollbar-thumb, .reading::-webkit-scrollbar-thumb { background: #D8D2C4; border-radius: 4px; }
        .msg-list::-webkit-scrollbar-track, .reading::-webkit-scrollbar-track { background: transparent; }
        .paper-grain { background-image: radial-gradient(rgba(28,26,22,0.025) 1px, transparent 1px); background-size: 4px 4px; }
        .row-hover { transition: background 160ms ease, box-shadow 160ms ease; }
        .kbd { font-size: 10px; padding: 1px 5px; border: 1px solid #D8D2C4; border-radius: 4px; color: #8A8270; background: #FBF9F4; box-shadow: 0 1px 0 #D8D2C4; }
      `}} />

      {/* Top bar */}
      <header className="h-[60px] flex items-center justify-between px-5 border-b border-[#E2DCCD] bg-[#FBF9F4] shrink-0">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-[9px] bg-[#1C1A16] flex items-center justify-center">
            <span className="serif text-[#EDE6D6] text-[15px] font-semibold leading-none mt-[1px]">M</span>
          </div>
          <div>
            <div className="serif text-[16px] font-semibold tracking-[-0.01em] leading-tight">Meridian Mail</div>
            <div className="text-[10.5px] text-[#8A8270] tracking-wide leading-tight">PRIVATE WORKSPACE</div>
          </div>
        </div>

        <div className="flex-1 max-w-[480px] mx-8 relative">
          <Search className="w-[15px] h-[15px] text-[#9B937F] absolute left-3.5 top-1/2 -translate-y-1/2" />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="Search mail, people, attachments…"
            className="w-full h-[38px] pl-10 pr-16 rounded-[10px] bg-[#F0EBDF] border border-transparent focus:border-[#2F5D44] focus:bg-white outline-none text-[13px] placeholder:text-[#9B937F] transition-colors"
          />
          <span className="kbd absolute right-3 top-1/2 -translate-y-1/2">⌘ K</span>
        </div>

        <div className="flex items-center gap-4">
          <div className="flex items-center gap-1.5 text-[11.5px] text-[#2F5D44] font-medium bg-[#E7EFE9] px-2.5 py-1.5 rounded-full">
            <ShieldCheck className="w-3.5 h-3.5" />
            End-to-end encrypted
          </div>
          <div className="flex items-center gap-2.5">
            <div className="text-right hidden md:block">
              <div className="text-[12.5px] font-medium leading-tight">Marcus Hale</div>
              <div className="text-[11px] text-[#8A8270] leading-tight">marcus@halegroup.com</div>
            </div>
            <div className="w-9 h-9 rounded-full bg-[#2F5D44] text-[#EDE6D6] flex items-center justify-center text-[12px] font-semibold ring-2 ring-[#E2DCCD]">MH</div>
          </div>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        {/* Sidebar */}
        <aside className="w-[228px] shrink-0 border-r border-[#E2DCCD] bg-[#FBF9F4] flex flex-col">
          <div className="p-4">
            <button className="w-full h-[42px] rounded-[10px] bg-[#1C1A16] text-[#F4F1EA] text-[13px] font-medium flex items-center justify-center gap-2 hover:bg-[#322E27] transition-colors shadow-[0_2px_8px_rgba(28,26,22,0.18)]">
              <PenSquare className="w-4 h-4" />
              Compose
            </button>
          </div>

          <nav className="px-2.5 space-y-0.5">
            {folders.map(f => {
              const active = activeFolder === f.id;
              return (
                <button
                  key={f.id}
                  onClick={() => setActiveFolder(f.id)}
                  className={`w-full flex items-center gap-2.5 px-3 h-[36px] rounded-[8px] text-[13px] transition-colors ${
                    active ? 'bg-[#EDE7D9] font-medium text-[#1C1A16]' : 'text-[#5B5547] hover:bg-[#F0EBDF]'
                  }`}
                >
                  <f.icon className={`w-[16px] h-[16px] ${active ? 'text-[#2F5D44]' : 'text-[#9B937F]'}`} strokeWidth={active ? 2.2 : 1.8} />
                  <span className="flex-1 text-left">{f.label}</span>
                  {f.id === 'inbox' && unreadCount > 0 && (
                    <span className="text-[11px] font-semibold text-[#2F5D44] bg-[#E7EFE9] px-1.5 py-0.5 rounded-full">{unreadCount}</span>
                  )}
                  {f.id === 'starred' && <span className="text-[11px] text-[#9B937F]">{starred.size}</span>}
                </button>
              );
            })}
          </nav>

          <div className="mt-6 px-5">
            <div className="flex items-center justify-between text-[10.5px] font-semibold tracking-[0.08em] text-[#9B937F] mb-2">
              <span>LABELS</span>
              <Tag className="w-3 h-3" />
            </div>
          </div>
          <div className="px-2.5 space-y-0.5">
            {labels.map(l => (
              <button key={l.name} className="w-full flex items-center gap-2.5 px-3 h-[32px] rounded-[8px] text-[12.5px] text-[#5B5547] hover:bg-[#F0EBDF] transition-colors">
                <span className="w-2 h-2 rounded-[3px]" style={{ background: l.color }} />
                {l.name}
              </button>
            ))}
          </div>

          <div className="mt-auto m-3 p-3.5 rounded-[10px] bg-[#F0EBDF] border border-[#E2DCCD]">
            <div className="flex items-center gap-2 text-[12px] font-medium mb-1">
              <Lock className="w-3.5 h-3.5 text-[#2F5D44]" />
              Vault storage
            </div>
            <div className="h-1.5 rounded-full bg-[#DDD6C5] overflow-hidden mb-1.5">
              <div className="h-full w-[34%] rounded-full bg-[#2F5D44]" />
            </div>
            <div className="text-[11px] text-[#8A8270]">17.2 GB of 50 GB · encrypted at rest</div>
          </div>
        </aside>

        {/* Message list */}
        <section className="w-[392px] shrink-0 border-r border-[#E2DCCD] flex flex-col bg-[#F7F4ED]">
          <div className="h-[52px] px-5 flex items-center justify-between border-b border-[#E2DCCD] shrink-0">
            <div className="flex items-baseline gap-2">
              <h2 className="serif text-[18px] font-semibold tracking-[-0.01em] capitalize">{activeFolder}</h2>
              <span className="text-[11.5px] text-[#9B937F]">{visible.length} threads</span>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setFilterUnread(v => !v)}
                className={`flex items-center gap-1.5 text-[11.5px] px-2.5 py-1.5 rounded-[7px] transition-colors ${filterUnread ? 'bg-[#1C1A16] text-[#F4F1EA]' : 'text-[#5B5547] hover:bg-[#EDE7D9]'}`}
              >
                <Filter className="w-3 h-3" /> Unread
              </button>
            </div>
          </div>

          <div className="msg-list flex-1 overflow-y-auto">
            {visible.length === 0 && (
              <div className="flex flex-col items-center justify-center h-full text-[#9B937F] gap-2 pb-20">
                <ArchiveX className="w-7 h-7" strokeWidth={1.5} />
                <span className="text-[13px]">Nothing here</span>
              </div>
            )}
            {visible.map(m => {
              const isSel = m.id === selectedId;
              const isUnread = !readIds.has(m.id);
              return (
                <button
                  key={m.id}
                  onClick={() => openMessage(m.id)}
                  className={`row-hover w-full text-left px-5 py-[15px] border-b border-[#ECE7DA] relative ${
                    isSel ? 'bg-[#FBF9F4] shadow-[inset_3px_0_0_#2F5D44]' : 'hover:bg-[#F2EEE3]'
                  }`}
                >
                  <div className="flex items-start gap-3">
                    <div className="relative shrink-0 mt-0.5">
                      <div className="w-[38px] h-[38px] rounded-full flex items-center justify-center text-[12px] font-semibold text-[#F4F1EA]" style={{ background: m.avatarBg }}>
                        {m.initials}
                      </div>
                      {isUnread && <span className="absolute -top-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-[#B5552D] ring-2 ring-[#F7F4ED]" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2 mb-[3px]">
                        <span className={`text-[13px] truncate ${isUnread ? 'font-semibold' : 'font-medium text-[#3E392F]'}`}>{m.sender}</span>
                        <span className="text-[11px] text-[#9B937F] shrink-0 flex items-center gap-1.5">
                          {m.encrypted && <Lock className="w-2.5 h-2.5 text-[#2F5D44]" />}
                          {m.time}
                        </span>
                      </div>
                      <div className={`text-[12.5px] truncate mb-[3px] ${isUnread ? 'font-medium text-[#1C1A16]' : 'text-[#5B5547]'}`}>{m.subject}</div>
                      <div className="text-[12px] text-[#8A8270] truncate leading-snug">{m.preview}</div>
                      <div className="flex items-center gap-2 mt-2">
                        <span className="text-[10px] font-medium px-1.5 py-[2px] rounded-[5px]" style={{ color: m.labelColor, background: m.labelColor + '1A' }}>{m.label}</span>
                        {m.attachments > 0 && (
                          <span className="text-[10.5px] text-[#9B937F] flex items-center gap-0.5"><Paperclip className="w-3 h-3" />{m.attachments}</span>
                        )}
                        <span
                          role="button"
                          onClick={(e) => toggleStar(m.id, e)}
                          className="ml-auto p-1 -m-1 rounded hover:bg-[#E8E2D2] cursor-pointer"
                        >
                          <Star className={`w-3.5 h-3.5 ${starred.has(m.id) ? 'fill-[#C9912E] text-[#C9912E]' : 'text-[#C5BEAC]'}`} />
                        </span>
                      </div>
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
        </section>

        {/* Reading pane */}
        <main className="flex-1 min-w-0 flex flex-col bg-[#FBF9F4] paper-grain">
          <AnimatePresence mode="wait">
            {selected && (
              <motion.div
                key={selected.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.22, ease: 'easeOut' }}
                className="flex flex-col flex-1 min-h-0"
              >
                {/* Toolbar */}
                <div className="h-[52px] px-6 flex items-center justify-between border-b border-[#E2DCCD] shrink-0">
                  <div className="flex items-center gap-1">
                    {[{ icon: Archive, label: 'Archive' }, { icon: Trash2, label: 'Delete' }, { icon: Clock, label: 'Snooze' }, { icon: BellOff, label: 'Mute' }].map((a, i) => (
                      <button key={i} title={a.label} className="w-8 h-8 rounded-[7px] flex items-center justify-center text-[#5B5547] hover:bg-[#EDE7D9] transition-colors">
                        <a.icon className="w-4 h-4" strokeWidth={1.8} />
                      </button>
                    ))}
                  </div>
                  <div className="flex items-center gap-3">
                    <span className="text-[11px] text-[#9B937F] hidden lg:flex items-center gap-1.5">
                      <span className="kbd">E</span> archive · <span className="kbd">R</span> reply
                    </span>
                    <button onClick={(e) => toggleStar(selected.id, e)} className="w-8 h-8 rounded-[7px] flex items-center justify-center hover:bg-[#EDE7D9] transition-colors">
                      <Star className={`w-4 h-4 ${starred.has(selected.id) ? 'fill-[#C9912E] text-[#C9912E]' : 'text-[#5B5547]'}`} />
                    </button>
                    <button className="w-8 h-8 rounded-[7px] flex items-center justify-center text-[#5B5547] hover:bg-[#EDE7D9] transition-colors">
                      <MoreHorizontal className="w-4 h-4" />
                    </button>
                  </div>
                </div>

                <div className="reading flex-1 overflow-y-auto">
                  <div className="max-w-[720px] mx-auto px-10 py-9">
                    {/* Subject */}
                    <div className="flex items-start justify-between gap-4 mb-6">
                      <h1 className="serif text-[26px] leading-[1.25] font-semibold tracking-[-0.015em]">{selected.subject}</h1>
                      <span className="mt-2 shrink-0 text-[10.5px] font-medium px-2 py-1 rounded-[6px]" style={{ color: selected.labelColor, background: selected.labelColor + '1A' }}>
                        {selected.label}
                      </span>
                    </div>

                    {/* Sender card */}
                    <div className="flex items-center gap-3.5 pb-6 border-b border-[#E8E2D2]">
                      <div className="w-11 h-11 rounded-full flex items-center justify-center text-[13px] font-semibold text-[#F4F1EA]" style={{ background: selected.avatarBg }}>
                        {selected.initials}
                      </div>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[14px] font-semibold">{selected.sender}</span>
                          {selected.verified && <ShieldCheck className="w-3.5 h-3.5 text-[#2F5D44]" />}
                          <span className="text-[12px] text-[#9B937F]">· {selected.org}</span>
                        </div>
                        <div className="text-[12px] text-[#8A8270] flex items-center gap-2 mt-0.5">
                          to me
                          <ChevronDown className="w-3 h-3" />
                          {selected.encrypted && (
                            <span className="flex items-center gap-1 text-[#2F5D44]"><Lock className="w-3 h-3" /> Encrypted</span>
                          )}
                        </div>
                      </div>
                      <div className="text-right">
                        <div className="text-[12px] text-[#8A8270]">{selected.time === 'Yesterday' || selected.time === 'Mon' ? selected.time : `Today, ${selected.time}`}</div>
                        <div className="text-[11px] text-[#2F5D44] flex items-center justify-end gap-1 mt-0.5">
                          <CheckCheck className="w-3.5 h-3.5" /> Read receipt sent
                        </div>
                      </div>
                    </div>

                    {/* Body */}
                    <div className="py-7 space-y-5">
                      {selected.body.map((p, i) => (
                        <p key={i} className="text-[14.5px] leading-[1.75] text-[#2B2820]" style={{ fontWeight: 450 }}>{p}</p>
                      ))}
                      <p className="text-[14px] leading-[1.7] text-[#5B5547] whitespace-pre-line pt-2">
                        Best regards,{'\n'}{selected.signoff}
                      </p>
                    </div>

                    {/* Attachments */}
                    {selected.files.length > 0 && (
                      <div className="border-t border-[#E8E2D2] pt-5 pb-2">
                        <div className="text-[11px] font-semibold tracking-[0.08em] text-[#9B937F] mb-3">
                          {selected.files.length} ATTACHMENT{selected.files.length > 1 ? 'S' : ''} · SCANNED &amp; VERIFIED
                        </div>
                        <div className="flex flex-wrap gap-2.5">
                          {selected.files.map((f, i) => (
                            <button key={i} className="group flex items-center gap-3 pl-3 pr-4 py-2.5 rounded-[10px] bg-white border border-[#E2DCCD] hover:border-[#2F5D44] hover:shadow-[0_2px_10px_rgba(47,93,68,0.10)] transition-all text-left">
                              <div className="w-9 h-9 rounded-[8px] bg-[#F0EBDF] group-hover:bg-[#E7EFE9] flex items-center justify-center transition-colors">
                                <FileText className="w-4 h-4 text-[#2F5D44]" strokeWidth={1.8} />
                              </div>
                              <div>
                                <div className="text-[12.5px] font-medium max-w-[220px] truncate">{f.name}</div>
                                <div className="text-[11px] text-[#9B937F]">{f.size}</div>
                              </div>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Reply box */}
                    <div className="mt-8 mb-4 rounded-[14px] border border-[#E2DCCD] bg-white shadow-[0_1px_2px_rgba(28,26,22,0.04),0_8px_28px_-12px_rgba(28,26,22,0.12)] overflow-hidden">
                      <div className="px-5 pt-4 pb-3 flex items-center gap-2 text-[12.5px] text-[#5B5547] border-b border-[#F0EBDF]">
                        <Reply className="w-3.5 h-3.5" />
                        Replying to <span className="font-medium text-[#1C1A16]">{selected.sender}</span>
                        <span className="ml-auto flex items-center gap-1 text-[11px] text-[#2F5D44]"><Lock className="w-3 h-3" /> Encrypted reply</span>
                      </div>
                      <textarea
                        rows={3}
                        placeholder={`Write your reply… Meridian will match your usual tone.`}
                        className="w-full px-5 py-4 text-[13.5px] leading-relaxed outline-none resize-none placeholder:text-[#A8A18D] bg-transparent"
                      />
                      <div className="px-4 pb-4 flex items-center justify-between">
                        <div className="flex items-center gap-1">
                          <button className="w-8 h-8 rounded-[7px] flex items-center justify-center text-[#8A8270] hover:bg-[#F0EBDF] transition-colors"><Paperclip className="w-4 h-4" /></button>
                          <button className="w-8 h-8 rounded-[7px] flex items-center justify-center text-[#8A8270] hover:bg-[#F0EBDF] transition-colors"><Clock className="w-4 h-4" /></button>
                        </div>
                        <div className="flex items-center gap-2">
                          <button className="h-9 px-4 rounded-[9px] text-[13px] font-medium text-[#5B5547] hover:bg-[#F0EBDF] transition-colors flex items-center gap-1.5">
                            <Forward className="w-3.5 h-3.5" /> Forward
                          </button>
                          <button className="h-9 px-5 rounded-[9px] bg-[#2F5D44] text-[#F4F1EA] text-[13px] font-medium hover:bg-[#264C38] transition-colors flex items-center gap-2 shadow-[0_2px_8px_rgba(47,93,68,0.3)]">
                            Send reply
                            <Send className="w-3.5 h-3.5" />
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}