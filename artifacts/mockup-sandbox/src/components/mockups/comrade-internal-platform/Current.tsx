import { useState } from 'react';
import './_group.css';

const people = [
  ['Maya Chen', 'MC', 'you'],
  ['Rowan Lee', 'RL', 'lead'],
  ['Nia Okafor', 'NO', ''],
  ['Theo Martin', 'TM', ''],
];

function Avatar({ initials, ai = false }: { initials: string; ai?: boolean }) {
  return ai ? (
    <img
      className="ci-orb"
             src="/__mockup/images/comrade-internal-platform-brand-orb.png"
      alt="Comrade"
      style={{ objectFit: 'cover' }}
    />
  ) : (
    <span className="ci-avatar">{initials}</span>
  );
}

function Message({
  name, initials, time, children, ai = false, assisted = false, mine = false,
}: { name: string; initials: string; time: string; children: React.ReactNode; ai?: boolean; assisted?: boolean; mine?: boolean }) {
  return (
    <article
      className={`ci-message ${ai ? 'ci-message-ai' : ''} ${mine ? 'ci-message-mine' : ''}`}
      style={{
        justifyContent: mine ? 'flex-end' : 'flex-start',
        flexDirection: mine ? 'row-reverse' : 'row',
        textAlign: mine ? 'right' : 'left',
      }}
    >
      <Avatar initials={initials} ai={ai} />
      <div className="ci-message-copy">
        <div className="ci-message-meta">
          <strong>{name}</strong>{ai && <span className="ci-ai-badge">AI · SEEN BY ALL</span>}
          {assisted && <span className="ci-assisted">drafted with Comrade</span>}
          <span className="ci-time">{time}</span>
        </div>
        <div className="ci-body">{children}</div>
      </div>
    </article>
  );
}

export function Current() {
  const [comradeActive, setComradeActive] = useState(true);
  const [approved, setApproved] = useState(false);
  const [details, setDetails] = useState(false);
  const [layout, setLayout] = useState('classic');
  const [draft, setDraft] = useState('');
  const [sent, setSent] = useState(false);

  return (
    <main className="comrade-current">
      <nav className="ci-sidebar">
        <div className="ci-brand">
          <img
            className="ci-brand-mark"
             src="/__mockup/images/comrade-internal-platform-brand-orb.png"
            alt="Comrade"
            style={{ objectFit: 'cover' }}
          />
          <b>comrade</b>
        </div>
        <div className="ci-team-name">Northstar</div>
        <button className="ci-switch">switch team</button>

        <button className="ci-comrade-card"><Avatar initials="" ai /><span><b>Comrade</b><small>ready in your thread</small></span></button>
        <div className="ci-nav">
          <button><i>01</i> Tasks</button>
          <button><i>02</i> Team wiki</button>
          <button><i>03</i> Documents</button>
        </div>

        <div className="ci-section-heading">Threads <button>+</button></div>
        <div className="ci-threads">
          <button><i>T</i> General</button>
          <button className="active"><i>T</i> Ship the onboarding flow</button>
          <button><i>R</i> Design review</button>
        </div>

        <div className="ci-section-heading">Members</div>
        <div className="ci-members">{people.map(([name, initials, role]) => <div key={name}><Avatar initials={initials} /><span>{name}</span><em>{role}</em></div>)}</div>

        <div className="ci-signals">
          <span>Live signals</span>
          <p><b>+</b> memory compiled · +3 facts</p>
          <p>• launch readiness in 4 days</p>
          <button><i>04</i> Project setup</button>
          <button><i>05</i> Membership</button>
          <button><i>↗</i> Sign out</button>
        </div>
      </nav>

      <section className="ci-room">
        <header className="ci-room-header">
          <div><h1>Ship the onboarding flow</h1><p>Team-visible · work thread</p></div>
          <div className="ci-layouts">{['classic', 'split', 'board'].map(item => <button onClick={() => setLayout(item)} className={layout === item ? 'selected' : ''} key={item}>{item}</button>)}</div>
        </header>
        <div className="ci-room-body">
          <div className="ci-conversation">
            <div className="ci-chat">
              <div className="ci-day">TODAY · 09:14</div>
              <Message name="Rowan Lee" initials="RL" time="09:14">
                I’ve put the final onboarding checkpoints in the brief. Can we make sure the invite flow and the empty state land together?
              </Message>
              <Message name="Maya Chen" initials="MC" time="09:18" assisted mine>
                I can take the invite states. The analytics event is still the open question — are we tracking completion after the first project or after the first task?
              </Message>
              <Message name="Comrade" initials="" ai time="09:19">
                The team’s earlier decision was to count a member as activated after their first confirmed task. The current spec says the same thing in <a>Onboarding v2</a>.
              </Message>

              <section className="ci-activity">
                <button onClick={() => setDetails(!details)}><span>{details ? '−' : '+'}</span> Checked team wiki <em>result</em></button>
                {details && <pre>{'{\n  "page": "Onboarding v2",\n  "finding": "activation = first confirmed task"\n}'}</pre>}
              </section>

              <Message name="Nia Okafor" initials="NO" time="09:24">
                Great. I’ve added the screen copy and marked the handoff task ready for review.
              </Message>

              <section className="ci-consent">
                <div className="ci-consent-top"><span>Comrade wants to create a task</span><b>REVERSIBLE</b><em>{approved ? 'EXECUTED' : 'AWAITING YOUR APPROVAL'}</em></div>
                <div className="ci-consent-body">
                  <blockquote>“Create a final QA pass for invite and empty states.”</blockquote>
                  <div className="ci-code"><p><b>action</b> &nbsp;&nbsp; Create a team task</p><p><b>title</b> &nbsp;&nbsp; QA onboarding invite &amp; empty states</p><p><b>assignee</b> Maya Chen</p></div>
                  {!approved ? <div className="ci-consent-actions"><button onClick={() => setApproved(true)} className="ci-primary">ALLOW ONCE</button><button>ALLOW FOR THIS THREAD</button><button>Edit</button><button className="ghost">Reject</button><small>EXPIRES IN 14:32</small></div> : <div className="ci-executed">✓ Executed</div>}
                </div>
                {approved && <div className="ci-stamp">DONE</div>}
              </section>
            </div>
            <div className="ci-composer">
              <button
                type="button"
                aria-pressed={comradeActive}
                aria-label={comradeActive ? 'Comrade is active. Switch to the team.' : 'Comrade is inactive. Switch to Comrade.'}
                onClick={() => setComradeActive(active => !active)}
                style={{ width: 30, height: 30, border: 0, padding: 0, borderRadius: '50%', background: comradeActive ? 'rgba(212,90,66,.16)' : 'transparent', opacity: comradeActive ? 1 : .55, cursor: 'pointer', boxShadow: comradeActive ? '0 0 0 3px rgba(212,90,66,.18)' : 'none' }}
              >
                <img src="/__mockup/images/comrade-internal-platform-brand-orb.png" alt="" aria-hidden style={{ width: 30, height: 30, display: 'block', objectFit: 'cover', borderRadius: '50%' }} />
              </button>
              <input value={draft} onChange={e => setDraft(e.target.value)} placeholder={comradeActive ? 'Ask Comrade…' : 'Message the team…'} onKeyDown={e => { if (e.key === 'Enter' && draft) { setSent(true); setDraft(''); } }} />
              <button className="ci-send" onClick={() => { if (draft) { setSent(true); setDraft(''); } }}>SEND</button>
            </div>
            {sent && <div className="ci-local-note">{comradeActive ? 'Comrade will respond in this visual preview.' : 'Message ready for the team in this visual preview.'}</div>}
          </div>
          <aside className="ci-panel">
            <section><label>Next deadline</label><div className="ci-deadline"><strong>4</strong><p>days until<br /><b>Launch readiness</b><br /><span>FRI, 18 OCT</span></p></div></section>
            <section><label>Work state</label><div className="ci-state"><span></span> ACTIVE <small>6 tasks in motion</small></div></section>
            <section><label>Team plan</label><ol><li className="done">Scope complete <small>09 Oct</small></li><li className="now">Build invite states <small>in progress</small></li><li>QA &amp; instrumentation <small>next</small></li><li>Launch review</li></ol></section>
            <section><label>Task pulse</label><div className="ci-task"><span className="square done"></span><p><b>Invite flow states</b><small>Maya · in progress</small></p></div><div className="ci-task"><span className="square review"></span><p><b>Empty-state copy</b><small>Nia · review</small></p></div><div className="ci-task"><span className="square"></span><p><b>QA onboarding paths</b><small>Proposed by Comrade</small></p></div></section>
            <section><label>Recent documents</label><a className="ci-document">Onboarding v2 <span>ready</span></a><a className="ci-document">Launch checklist <span>ready</span></a></section>
          </aside>
        </div>
      </section>
    </main>
  );
}