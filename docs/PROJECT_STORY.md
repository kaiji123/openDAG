## Inspiration

Most epidemiology papers never publish their causal assumptions. A study reports that X is linked to Y "after adjustment", but the list of what was adjusted for is usually buried in a methods paragraph or a table footnote, and nobody checks it against what *should* have been adjusted for. A 2021 review of DAG use in health research (Tennant et al., IJE) found that reporting of causal diagrams varies widely, and most papers never draw one at all.

Causal diagrams (DAGs) are the standard way epidemiologists make these assumptions explicit, and tools like DAGitty make them easy to analyse. What's missing is the diagrams themselves. We wanted to see whether NVIDIA Nemotron could rebuild them from published papers, and whether a dataset of them would show patterns across a whole literature.

## What it does

OpenDAG turns an open-access health study into a causal diagram, flags what it probably missed, and adds it to an open dataset.

1. **Paste a DOI or PMCID.** OpenDAG fetches the paper from Europe PMC and accepts only Creative Commons-licensed papers. Full text is never stored.
2. **Nemotron 3 Nano extracts the causal diagram:** exposure, outcome, every covariate the authors adjusted for (with a short supporting quote), the study design and the main effect estimate.
3. **Nemotron 3 Super critiques it:** likely missing confounders and bias risks (immortal time bias, confounding by indication, selection bias…), with an overall assessment.
4. **Super then verifies each suggestion against the paper's own text.** It must quote the sentence showing an adjustment, and code checks that the quote really is in the paper. Suggestions the paper already covered move into the diagram as adjusted, so false alarms don't reach the user.
5. **Tavily finds evidence** for every remaining gap, linking to the literature.
6. **A person reviews it:** accept or reject each suggestion, edit the diagram, approve. Papers reviewed by Nemotron instead are clearly labelled "AI-reviewed".

The dataset then becomes useful on its own:
- **A forest plot** of every study's effect estimate, and a chart of **the most often missed confounders**. In our first 16 papers, socioeconomic status was flagged as missing in 10.
- **Ask the dataset** in plain English ("Which papers did not adjust for smoking?"). Super turns the question into an exact query, code runs it over the records, and Nano writes a cited answer.
- **A 3D network** of every variable across all papers. Nano merges names that mean the same thing ("age at baseline", "Age (years)") and attaches MeSH terms, each verified against the National Library of Medicine.
- **Exports** to JSONL and DAGitty, with the code under MIT and the dataset under CC BY 4.0.

## How we built it

- **Backend:** Python + FastAPI, SQLite, networkx for graph validation (acyclic, valid edges) and backdoor-path checks.
- **Models:** NVIDIA Nemotron 3 Nano and Nemotron 3 Super on Nebius Token Factory, through its OpenAI-compatible API. Each model is matched to its job: Nano for high-volume extraction, name merging and writing answers; Super, with reasoning on, where precision matters (critique, verification, query planning).
- **Evidence:** the Tavily search API, with results cached, deduplicated, and the paper itself filtered out so it can't be cited against itself.
- **Data sources:** the Europe PMC REST API, NCBI's ID converter and the NLM MeSH lookup.
- **Frontend:** plain HTML and JavaScript, Cytoscape.js for the DAGs (drawn in the classic epidemiology layout: confounders above, exposure → outcome in the middle, AI-suggested gaps below), 3d-force-graph for the network, GSAP for motion.
- **Running it safely in public:**
  - a hard spending cap on every model and Tavily call, with real token usage recorded and kept in Upstash Redis so restarts can't reset it
  - per-visitor rate limits
  - email alerts via Resend for budget thresholds, model failures and unexpected errors
- **Deployment:** Docker on Render. The demo film was made with HyperFrames from scripted recordings of the real app.

## Challenges we ran into

- **The model missed covariates in long footnote lists.** Nano would extract 16 of 30 covariates from a "Model 2 was adjusted for…" footnote, and the critique then "discovered" the dropped ones as missing. That's a false alarm that looks like an insight. We fixed it with a stricter extraction prompt and the verification step, which checks every suggestion against the paper's text with code-checked quotes.
- **Small models are bad at scanning lists.** Our first "Ask the dataset" had Nano answer straight from the records. For "Which papers didn't adjust for smoking?", half the papers it named *had* adjusted for smoking. We switched to Super writing a structured query, code doing the filtering exactly, and Nano only phrasing the result. It then matched the ground truth on every test question.
- **AI review isn't consistent.** Our AI reviewer rejected smoking as a confounder in one run and accepted it in the next. This matches published findings (Huntington-Klein & Murray, 2024), and it's why OpenDAG labels AI decisions and keeps a person in the loop.
- **Real-world API issues:**
  - Europe PMC's search was down for DOI queries, so we added an NCBI fallback.
  - Some "open access" COVID-era papers have no licence, so we required a Creative Commons one.
  - Token Factory returned "529 worker overloaded" under load, so we added retries with timeouts.
- **Cost control without a spend API.** Token Factory doesn't expose spending, and budget alerts don't stop usage. So we built a ledger from the token counts each response returns, checked against a worst-case estimate before every call. It fails closed if the ledger can't be read.
- **Free hosting wipes the disk on restart.** We reload approved papers from the repo on startup and keep the spending ledger in Upstash.

## Accomplishments that we're proud of

- A pipeline that **checks itself**: the critique's suggestions are verified against the source text, so the AI's claims are grounded in the paper.
- **Every number in the demo is real:** a paper processed live, with its real hazard ratio (1.20, 95% CI 1.07–1.35), real suggestions and real evidence links.
- A dataset view that shows something a single paper can't: **socioeconomic status flagged as missing in 10 of 16 studies**.
- Grounded Q&A whose answers show how they were computed ("Filtered 16 papers: did not adjust for smoking → 6 match"), with citations to real records only.
- Building and testing the whole thing for **about $0.15 of Nemotron usage**, with a hard cap protecting the public demo.

## What we learned

- **Let code do the counting.** LLMs are excellent at understanding a question and phrasing an answer, and unreliable at filtering a list. Splitting those jobs fixed accuracy.
- **Match the model to the job.** Nano was the right choice for volume, and Super was clearly better at precise checks such as finding "education" in a 30-item footnote.
- **Verify model claims against the source.** Requiring a quote, and checking it with code, turned a plausible-sounding critique into a trustworthy one.
- **Make AI involvement visible.** Labelling what came from the paper, from the AI and from a reviewer matters more to researchers than polish.

## What's next for openDAG

- **More papers:** growing the dataset field by field (cardiovascular, pharmacoepidemiology, nutrition), with community review.
- **Durable storage:** moving from SQLite on a free host to Postgres, so contributions made on the live site are kept.
- **Adjustment-set analysis:** computing minimal sufficient adjustment sets directly and comparing them with what each paper did.
- **Better evidence:** restricting Tavily to scholarly sources and extracting a one-line finding from each.
- **Reviewer accounts:** so experts can review AI suggestions, building a gold-standard set for measuring how well the models do.
