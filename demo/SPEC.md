# SPEC: OpenDAG demo, storyline A "The headline check"

GOAL: a researcher who has never heard of OpenDAG watches one real paper go in, sees the confounder its
authors didn't adjust for, and leaves knowing the dataset finds that gap across the literature.

OUTPUT: HyperFrames, 1920 x 1080, 30 fps.

TIME: ~52 s = ~1560 frames (exact length set by the recording). No music; effects only.

LOOK (the product's own, taken from static/style.css):
- ground #f7f7f4 (the app's background), cards #ffffff, ink #1d2327, muted #5f6b73, hairline #e2e3dd.
- DAG roles keep their meaning: exposure #1b8a6b, outcome #c4462f, adjusted #2f5d8a, AI-suggested #d08a00.
- accent = AI amber #d08a00, rationed: the suggested confounder, the "missed" number, the end-card mark's line.
- end card ground: the app header's ink #1d2327, mark ◇→◇ in #7fd4b8 (the header logo), wordmark #ffffff.
- type: Inter (closest bundled face to the app's system sans). Hook title 900, 76 px, -2% tracking, max 1500 px
  wide; question line 700, 54 px. Captions 800, 44 px, -1.5% tracking, ink pill (#1d2327, 18 px radius,
  22/34 px padding) bottom-left at 72 px / 64 px, white text, one amber word at most.
- medium: the real app recorded at 1440 x 810 CSS px, 2x device pixels; real NCBI PMC article page for the
  paper. Shadows: captions 0 10 40 rgba(0,0,0,.18) only.

OBJECTS (all real, nothing drawn to stand in for the product):
- the paper: "Regular proton pump inhibitor use and incident dementia: population-based cohort study"
  (BMC Medicine 2022, doi 10.1186/s12916-022-02478-y, CC BY), on its PMC page and as the hook's title text.
- OpenDAG's add-paper box, progress steps, record page (key figures, DAG, suggestions with Tavily links),
  dataset page (forest plot, most-often-missed chart).
- the cursor: drawn in the film from the recording's real path (big: 34 x 46 px), ink with a white edge.

MOTION:
- camera (zoom/pan on the recording): power2.inOut, 0.5-0.8 s, holds 2-4 s while the UI acts; zoom 1.0-1.9x.
- captions: whole line blurs in (8 px -> 0, opacity 0 -> 1, 0.2 s power4.out), dead still, blurs out 0.15 s power3.in.
- hook title: per word grey (#c9ccc8) -> ink, 0.08 s apart, no movement; question line blurs in as a line.
- the waiting time while the models run is compressed 5x in the recording (the process runs fast); every other
  moment plays at real speed.
- end card: colour flood 0.4 s power2.inOut; claim words grey -> white 0.1 s apart; mark pops (back.out(1.6),
  0.33 s), its arrow draws (0.6 s), wordmark wipes in (clip-path, 0.53 s power3.out), URL blurs in.

SHOTS (film time; final numbers come from the recording's marks):
1. 0.0-3.6 HOOK. Ground #f7f7f4. The paper's title builds word by word; "Should you trust it?" lands under it at
   2.2 s. Sound: none until the question's tap.
2. 3.6-9.5 TENSION. Blur swap into the PMC page; camera pushes to the methods paragraph listing covariates
   (1.6x). Caption: "Which confounders did they adjust for?" Sound: light tap on the caption, glide on the push.
3. 9.5-14 REVEAL. OpenDAG's add page; the cursor clicks the box and types the DOI; clicks Extract DAG.
   Caption: "Paste the DOI into OpenDAG". Sound: click, a key per character, click.
4. 14-21 PROOF, the pipeline (compressed 5x): the four progress steps tick. Captions in turn:
   "Nemotron Nano maps the causal diagram" / "Nemotron Super finds what's missing" / "Tavily finds the evidence".
5. 21-33 PROOF, the result: the record page lands; camera on the key figures (HR + CI), then the DAG; the cursor
   clicks an amber AI-suggested node; its rationale and Tavily links open; the cursor clicks Accept.
   Caption: "Not adjusted for, with sources". Sound: clicks, a pop on the node detail.
6. 33-44 PAYOFF. Back to the dataset (show drafts ticked): camera on the forest plot, the new PPI-dementia row
   among the other PPI studies (hover tooltip); then the "most often missed" chart's top bar (hover).
   Caption: "Socioeconomic status: missed in N of M papers" (the real numbers at recording time).
7. 44-47 CLAIM, on the end card's flood: "Every study's assumptions, made visible".
8. 47-52 END CARD. Mark ◇→◇ pops, wordmark "OpenDAG" wipes in, the repo URL lands, holds 2.5 s.
   Sound: whoosh into the flood, pop on the mark, one glockenspiel note as the wordmark completes.

BANNED: a dark void with floating UI; glow, particles, gradients; three cards in a row; a title over a dimmed
screenshot; any invented UI, number or label; the logo docked in a corner; captions that move while read;
the same entrance for every caption and title; music (Kai prefers effects only); typing into an empty prompt box
as the opener.

QA frames: 1.0 (title building), 3.0 (question landed), 6.5 (methods paragraph readable at 1.6x), 11 (DOI being
typed), 17 (progress steps legible), 24 (key figures), 29 (node detail with links), 38 (forest plot row + tooltip),
42 (missed bar), 45.5 (claim), 50.5 (end card complete). Every caption on screen >= 0.5 s + 0.3 s/word.
