"""The held-out set for stage-1 recall (findings §20.3.2).

Small on purpose. The metric exists to notice a regression, and a set nobody
will re-label is a set that rots — three sources with hand-labelled facts gives
a usable signal at a size a person can actually audit when it disagrees.

The labels are chosen to probe STARVATION, not to be easy. Anyone can extract
"the deadline is 14 March" from a line that says so. The interesting question
is what a single-call extractor drops when the fact is:

  * buried mid-paragraph rather than in a heading or a bullet,
  * stated as a NEGATIVE — a constraint about what the team is not doing,
  * SUPERSEDED in the same source ("was X, now Y"), where the extractor has to
    carry the current value rather than the first one it read,
  * a threshold or number embedded in prose,
  * an ownership assignment expressed as a person's aside rather than a
    declaration.

Each of those is a fact that, once missed, is unreachable forever — there is no
vector fallback and no raw-text search behind stage 1. That is the whole
argument of §20.3.2, made concrete enough to measure.

Bodies are written as a team would write them, not as a test fixture would:
mixed formatting, incidental noise, facts out of order. A clean bulleted source
would measure the extractor's best case and tell us nothing about the one that
matters.
"""
from evaluation.extraction import Document, ExpectedFact as F

PROJECT_BRIEF = Document(
    name="brief.md",
    kind="document",
    body="""# MealShare — project brief

We're building a way for people in the same building to share surplus cooked
food. Think "leftovers, but coordinated".

## Scope

The pilot covers one building only. We had originally scoped this to the whole
campus, but after talking to facilities in week 2 we cut it back to Ashworth
House — campus-wide is out of scope for the pilot and we should stop describing
it that way in demos.

Riya is running the backend. She's already set up the repo. Tom said he'd take
the interface work as long as someone else does the copy, and Dee volunteered
for that in the same conversation.

## Constraints

We are not using Firebase. The department won't sign off on US-hosted data for
student projects, so anything we pick has to be EU-hosted — that ruled out our
first two candidates.

A listing expires after 90 minutes. That came out of the food-safety guidance
Dee found; it's not negotiable and the UI needs to make the countdown obvious.

## Dates

Demo day is 14 March. Code freeze is the Monday before.
""",
    expected=(
        F("The pilot covers Ashworth House only, not the whole campus",
          ("ashworth",)),
        F("Campus-wide scope was cut after week 2 and is out of scope",
          ("campus",)),
        F("Riya is running the backend", ("riya", "backend")),
        F("Tom is doing the interface work", ("tom", "interface")),
        F("Dee is writing the copy", ("dee", "copy")),
        F("The team is not using Firebase", ("firebase",)),
        F("Data must be EU-hosted", ("eu",)),
        F("A listing expires after 90 minutes", ("90 minutes",)),
        F("Demo day is 14 March", ("demo", ("14 march", "march 14"))),
        F("Code freeze is the Monday before demo day", ("code freeze",)),
    ),
)

STANDUP_CHAT = Document(
    name="standup.txt",
    kind="chat",
    body="""[1] riya: morning. auth is done, merged last night
[2] tom: nice. i'm blocked on the listing card until the api shape settles
[3] riya: it's settled — /listings returns items not listings, i renamed it
[4] riya: sorry, should have said in here
[5] dee: is the 90 min countdown mine or tom's
[6] tom: yours i think? it's copy
[7] dee: ok taking it
[8] tom: also we should drop the map view for the pilot, nobody asked for it
[9] riya: agreed, cutting map
[10] dee: one thing — facilities want a named contact on the poster before we
put it up, i said it'd be me
[11] tom: 👍
[12] riya: i'll be away the week of the 3rd btw, back on the 10th
""",
    expected=(
        F("Auth is done and merged", (("auth", "authentication"),)),
        F("The /listings endpoint returns 'items', not 'listings'",
          ("items",)),
        F("Dee owns the 90-minute countdown copy", ("dee", "countdown")),
        F("The map view is cut from the pilot", ("map",)),
        F("Dee is the named contact on the poster", ("dee", "poster")),
        # `away` was the wrong key — the extractor wrote "unavailable", which
        # is the same fact. The load-bearing part is who and when.
        F("Riya is away the week of the 3rd, back on the 10th",
          ("riya", "3rd")),
    ),
)

REPO_ACTIVITY = Document(
    name="repo.txt",
    kind="github",
    body="""[1] merged pull request #12 by riya: add supabase auth — replaces the
placeholder login, closes #4
[2] opened issue #17 by tom: listing card overflows on small screens
[3] review on pull request #14 by dee: requested changes — copy on the expiry
banner still says 2 hours, should be 90 minutes
[4] merged pull request #15 by riya: rename listings to items across the api
[5] closed issue #9 by tom: won't fix — map view is out of scope for the pilot
""",
    expected=(
        # NOT keyed on "12". The github extractor carries provenance in
        # Candidate.source_index — the numbered transcript line — so requiring
        # the PR number inside the fact TEXT was the label demanding something
        # the pipeline deliberately keeps elsewhere. Measured: the extractor
        # produced the fact and scored a miss for it.
        F("Riya added Supabase auth in PR 12",
          ("supabase", ("auth", "authentication"))),
        F("The expiry banner copy said 2 hours and should say 90 minutes",
          ("90 minutes",)),
        F("Listings were renamed to items across the API in PR 15",
          ("items", "renamed")),
        F("The map view is out of scope and issue 9 was closed won't fix",
          ("map",)),
    ),
)

DOCUMENTS = (PROJECT_BRIEF, STANDUP_CHAT, REPO_ACTIVITY)
