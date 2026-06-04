# percona-dk value-claims audit

*Auditor pass, 2026-06-02. Model under test: Claude Opus 4.8 (1M). All facts below were
verified against the live corpus via `search_percona_docs` / `get_percona_doc` before
being written. A/B arms were run as fresh sub-agents of the same model.*

## TL;DR (lead with the honest finding)

Against a **web-search-enabled chat using this same strong model, the connector did not
produce a single clean unique win in five head-to-head tests.** Web search found the
right `docs.percona.com` / `forums.percona.com` URL every time, and on two questions it
was *more* correct than the README's own example text.

The connector's real, defensible value is narrower than the README claims, but it is
real:

1. **🔵 No-web agents / automation / air-gapped / CI** - the genuine, large win. Vanilla
   (this model, no tools, no web) was **flatly wrong on 2 of 5** questions, and an agent
   with no web has no fallback. This is the strongest honest case and the README already
   names it - it should *lead*, not sit fifth.
2. **Freshness on genuinely post-cutoff items** - PCSM 0.8.1 (2026-05-07), PXC Clone SST
   in 8.4.4-4 (2025-04-16), the 2026-05-02 redo-log blog are all in the corpus. A no-web
   model cannot know these. Demonstrable.
3. **Determinism, Percona-scoping, never inventing URLs, returning several ranked real
   threads with verified live URLs.** Real but modest - web search also returned real
   URLs in every test.
4. **Weaker tool-calling models** (Llama/Qwen/Mistral) that *do* carry the bad defaults
   the skills assume. Real - but must be stated as such, not implied for all models.

What did **not** reproduce with this strong model:
- "Web search invents dead URLs / flails on pinpointing" - it didn't; it found real
  threads, including a *better* one than the README cites.
- "Vanilla invents a dead forum URL" - it didn't; it honestly **declined** to give a URL.
- "Web search routinely mixes 8.0/8.4 branches" - neither vanilla nor web mixed branches
  on the redo-log test; both knew `innodb_log_file_size` is gone in 8.4.
- "Stale training data → wrong commands (mysqldump, innobackupex)" - this strong model
  already chose XtraBackup over mysqldump, knew `innobackupex` is removed, and led with
  `ALGORITHM=INSTANT` for a trailing nullable column.

---

## 1. Value matrix (from real tests)

Legend: ✅ correct · ❌ wrong · ⚠️ correct-but-narrowed/stale · — not run

| # | Question | Vanilla (no tools) | +Skill (no connector) | +Connector | +Web search | Honest tag |
|---|----------|-------------------|----------------------|-----------|-------------|-----------|
| Q1 | PXC Clone-plugin SST? since which version / port | ❌ "PXC does not use Clone as SST" | — | ✅ 8.4.4-4 (8.4); 8.0.41 default (8.0); port 4444 | ✅ "since 8.0.41", real 8.0 URL | **🔵** wins vs no-web; **web ties** |
| Q2 | Exact forum thread: PMM QAN no Mongo queries despite profiling | ✅ honestly declined to give URL | — | ✅ returns 7073 + 10577 + doc page | ✅ found 10577 (accepted answer) + 7692 | **🟡** web ties / slightly wins |
| Q3 | XtraBackup 8.4 `--compress` default; `innobackupex`? | ✅ ZSTD; removed | — | ✅ | ✅ cited | **🟡** tie (vanilla already right) |
| Q4 | Redo-log sizing PS 8.0 vs 8.4 (exact vars) | ✅ no version mix | — | ✅ | ✅ cited | **🟡** tie (vanilla already right) |
| Q5 | Atlas → self-hosted PSMDB minimal-downtime tool | ❌ recommended `mongosync`, "Percona has no dedicated tool" | — | ✅ PCSM/PLM + thread + FAQ | ✅ found PCSM + announcement | **🟡** web ties; wins vs vanilla |
| BD1 | ADD nullable col to 500 GB live table | ✅ led with INSTANT, offered pt-osc | ⚠️ pt-osc first, **dropped INSTANT** | — | — | skill **mild regression** |
| BD2 | Back up 300 GB online PS 8.0 | ✅ XtraBackup, rejected mysqldump | ✅ same | — | — | skill **no change** |

**Reading of the matrix:** the connector's wins are all **vs vanilla / no-web** (🔵), never
clean vs web (🟡). The two questions where vanilla was actually *wrong* (Q1, Q5) are the
real value - and they're real because a no-web agent can't recover from them.

---

## 2. Verified examples with real outputs

### Q1 — PXC Clone SST (README example #1, sold as 🟢) → actually 🔵/🟡

- **Vanilla (this model, no tools):** "No. Percona XtraDB Cluster does not use the MySQL
  Clone plugin as an SST method. PXC's supported SST methods are XtraBackup … and
  rsync … The Clone plugin is what Group Replication / InnoDB Cluster uses." → **WRONG.**
- **+Web:** "Yes … starting in PXC 8.0.41 … `clone` is in the default
  `wsrep_sst_allowed_methods` … default SST port 4444." Cited
  `https://docs.percona.com/percona-xtradb-cluster/8.0/clone-sst.html`. → **CORRECT.**
- **+Connector / corpus truth:**
  - `pxc-docs` **8.4** `docs/clone-sst.md`: "Introduced in Percona XtraDB Cluster 8.4.4-4
    … port 4444 by default". Release notes 8.4.4-4 (2025-04-16) cite
    [PXC-4469](https://perconadev.atlassian.net/browse/PXC-4469).
    URL: https://docs.percona.com/percona-xtradb-cluster/8.4/clone-sst/
  - `pxc-docs` **8.0** `docs/clone-sst.md` (tech-preview) + `wsrep-system-index.md`:
    "Percona XtraDB Cluster 8.0.41 includes `clone` to the default value … 8.0.20-11.3
    adds this variable."
    URL: https://docs.percona.com/percona-xtradb-cluster/8.0/wsrep-system-index/

**Finding:** Clone SST is **not** a 8.4.4-4-only novelty - it exists in PXC 8.0 (tech
preview; became default in 8.0.41). The README's framing ("New feature LLMs can't know
about", "web search … misses the PXC-specific implementation") is **not supported**: web
search nailed it with the correct PXC URL. The *real* win is vs **vanilla**, which denied
the feature outright. Tag should be **🔵** (or 🟡 vs web), not 🟢.

### Q5 — Atlas → PSMDB migration (README example #2, sold as 🟢) → 🟡 + STALE

- **Vanilla:** "The standard minimal-downtime approach is `mongosync` … Percona doesn't
  ship its own dedicated live-migration tool." → **WRONG** (misses Percona's own tool).
- **+Web:** "Percona ClusterSync for MongoDB (PCSM) … change streams … near-zero
  downtime", cited docs + announcement blog. → **CORRECT, current name.**
- **Corpus truth:** `pcsm-docs/docs/release-notes.md`: *"Starting with version 0.7.0,
  Percona Link for MongoDB has been **rebranded as Percona ClusterSync for MongoDB**."*
  Current release **PCSM 0.8.1 (2026-05-07)**. The README still says **"Percona Link for
  MongoDB 0.5.0"** and links the 0.5.0 release notes.

**Finding:** the README's flagship *freshness* example is itself **stale** - it names a
rebranded product by its old name and pins a superseded MVP version. Also, the README's
"zero-downtime … Atlas" wording is contradicted by the **accepted answer** in the very
forum thread it cites (36958, 2025-02-27, @Ivan_Groenewold): *"zero downtime is not
possible since Atlas does not expose the backend nodes."* PLM 0.5.0's own release notes do
claim zero-downtime-from-Atlas, so this is a genuine tool-marketing-vs-forum tension that
should be surfaced, not smoothed over. Tag → **🟡** (web ties); fix name/version.

### Q2 — Forum pinpoint (README example #3, sold as 🟢) → 🟡

- **Vanilla:** "I do not know the exact URL. I'm not going to construct one, because any
  forums.percona.com URL I produce would be fabricated." → **honest, no hallucination.**
- **+Web:** found `…/pmm-query-analytics-for-mongodb-not-showing-any-querys/10577`
  (a 2021 PMM-2.17 thread **with an accepted answer**) plus `/7692`. Verified by fetch.
- **Corpus truth:** the README cites **thread 7073** — but 7073 is a **2019 PMM-1.x**
  thread that was **never resolved** (posts #2–#5 are all "Can anyone help me, please?").
  Thread **10577** (accepted answer: "solved after updating my docker host") is the better
  match, and the connector *does* surface it (rank 4–7) when queried well.

**Finding:** "Web search rarely surfaces both [thread + doc] … web search loses here" is
**not supported** - web found a thread (a *better* one) and would trivially find the doc
page. And the predicted failure modes (vanilla invents a URL / web flails) **did not
occur**. The connector's genuine edge here is determinism + several ranked real threads +
never inventing URLs - modest, not "web loses." If the example is kept, cite **10577**,
not 7073.

### Q4 — Version scoping (README line 17 + example #5) → claim not reproduced

- **Vanilla:** "8.0: `innodb_log_file_size` + `innodb_log_files_in_group` … 8.0.30
  introduced `innodb_redo_log_capacity` … 8.4: use `innodb_redo_log_capacity`, the old
  ones are deprecated/removed." → **CORRECT, no branch-mixing.**
- **+Web:** same, cited.
- **Corpus truth:** matches (pxb/psmysql docs confirm `innodb_redo_log_capacity` is the
  8.4 knob; legacy vars removed).

**Finding:** "Web search routinely mixes branches; web search loses here" did **not**
reproduce. The `version=` filter is a real *retrieval-scoping* feature (chunks are
branch-tagged; URLs resolve to the correct `/8.4/` path) and genuinely useful for weaker
models and no-web agents - but I could not demonstrate it correcting a wrong-branch answer
from this strong model *or* from web search. Scope the claim accordingly. Note also that
README example #5 (Clone SST port for 8.4) is a poor version-scoping demo: the port (4444)
and Clone SST both exist in 8.0 too, so nothing diverges.

### Q3 / BD2 — XtraBackup (skill facts) → verified true, but ties

ZSTD default for `--compress` (pxb-docs 8.4 `xtrabackup-option-reference.md`,
`create-compressed-backup.md`), `innobackupex` removed in 8.0+, three-step
backup/prepare/copy-back: all **confirmed in corpus** and **all three arms got them
right**, including vanilla. These are 🟡 ties, not wins.

### Blog example #4 — accurate, keep

`percona-community-blog/posts/2026-02-01-…-variables-that-actually-matter.md`: section 1
is indeed **`innodb_buffer_pool_size`**. README claim verified accurate. (Correctly tagged
🟡 already.)

---

## 3. Overclaims to cut or soften (with evidence)

| Location | Claim | Problem (evidence) | Fix |
|---|---|---|---|
| README §"Proof", header | "five examples … 🟢 *beats web search*" | In A/B tests web matched or beat the connector on all five; 3 of the 3 🟢s did not beat web | Re-tag: #1→🔵/🟡, #2→🟡, #3→🟡. Keep only claims that survive the test |
| README #1 | "New feature LLMs can't know about"; "web search … misses the PXC-specific implementation" | Web found it with the right PXC URL; feature exists in PXC 8.0 (default since 8.0.41), not new in 8.4.4-4 | Reframe as "vanilla denies it exists" (true) + fix the 8.0/8.4 history |
| README #2 | "Percona Link for MongoDB 0.5.0" | Rebranded to **Percona ClusterSync for MongoDB (PCSM)** at 0.7.0; current 0.8.1 (2026-05-07) | Rename to PCSM, update version; this is a freshness example that is itself stale |
| README #2 | "zero-downtime … Atlas" | Contradicted by the accepted answer in the cited thread 36958 ("zero downtime is not possible … Atlas does not expose backend nodes") | Say "minimal-downtime"; surface the Atlas caveat instead of hiding it |
| README #3 | cites thread **7073** as "the exact thread"; "web search rarely surfaces both" | 7073 is a never-resolved 2019 PMM-1.x thread; thread **10577** (accepted answer) is better and web found it | Cite 10577; soften to "deterministic, multiple ranked real threads, no invented URLs" |
| README line 16 | "Web-search ranking flails … or invents dead URLs. **Web search loses here.**" | Did not reproduce; web found real URLs every time; vanilla declined rather than inventing | Drop "web search loses"; reframe as determinism/scoping |
| README line 17 | "Version-scoped answers … Web search routinely mixes branches. **Web search loses here.**" | Not reproduced on redo-log test; neither vanilla nor web mixed 8.0/8.4 | Reframe `version=` as retrieval-scoping; value is for weak/no-web models |
| README line 9, 21 | "Without DK … wrong package names, deprecated flags (`innobackupex`), missing safety checks" | This strong model already avoids all of these (chose XtraBackup, knew innobackupex removed, led with INSTANT) | Scope to weaker/older models + no-web agents; don't imply the base model is clueless |
| **SKILL `mysql-to-percona` line ~60** | lists **`innodb_track_changed_pages`** (Changed Page Tracking) as a current PS feature to accelerate incremental XtraBackup | Corpus: **deprecated PS 8.0.27, REMOVED PS 8.0.30** (psmysql-docs `changed-page-tracking.md`). The `xtrabackup-recipes` skill correctly says use `--page-tracking` instead - the two skills **contradict each other** | Remove from mysql-to-percona or mark removed; point to `--page-tracking` / `mysqlbackup_page_track` |
| **SKILL `percona-toolkit-recipes`** | "agents default to `ALGORITHM=INPLACE` and miss pt-osc"; pushes pt-osc as the answer | For a trailing nullable column, **`ALGORITHM=INSTANT`** is the genuinely best answer (metadata-only, no rebuild). Skill never mentions it; in BD1 the skill made a strong model's answer *worse* by suppressing INSTANT | Add an "INSTANT-first for qualifying ADD COLUMN, pt-osc for rebuilds" note |

---

## 4. Reproducible test prompts

**Arm definitions (same model, Opus 4.8):**
- *Vanilla:* "answer from your own knowledge only; do NOT use web search or any tools."
- *+Skill:* prepend the relevant `SKILL.md` text as authoritative reference; no tools.
- *+Connector:* allow `search_percona_docs` / `get_percona_doc`; no web.
- *+Web:* web search/fetch allowed; no Percona connector.

**Connector vs web (run each arm, diff the output):**
1. "Does Percona XtraDB Cluster support the MySQL Clone plugin as an SST method? Since
   which PXC version, and what's the default SST port?"
2. "Link me the exact forums.percona.com thread where someone reported PMM Query Analytics
   shows no MongoDB queries even though profiling is enabled."
3. "In Percona XtraBackup 8.4, what's the default `--compress` algorithm, and is
   `innobackupex` still available in 8.x?"
4. "In Percona Server, how do I size the InnoDB redo log in 8.0 vs 8.4? Name the exact
   variables." (run connector with and without `version=`)
5. "What Percona tool migrates MongoDB Atlas → self-hosted Percona Server for MongoDB with
   minimal downtime?"

**Skill (no connector) vs vanilla — known-bad-default probes:**
- BD1: "Add a nullable column to a 500 GB live, write-heavy Percona Server 8.0 table with
  minimal disruption. Give the command."
- BD2: "Best way to back up a 300 GB Percona Server 8.0 database that must stay online?"

**What to watch:** invented/dead URLs, wrong branch quoted as current, the Percona-specific
tool overlooked (vanilla), and whether the skill changes a *correct* strong-model answer.

---

## 5. Proposed honest rewrite of the README value section

> ## Why this matters
>
> **The fair objection — "can't an AI just web-search the docs?"** For a strong model with
> web search, usually yes. [docs.percona.com](https://docs.percona.com),
> [percona.community](https://percona.community/blog/), and
> [forums.percona.com](https://forums.percona.com) are public, and in head-to-head tests a
> web-enabled chat reached the right Percona page on essentially every question we threw at
> it. We're not going to pretend otherwise. Here's the honest map of where percona-dk is a
> real win and where it just ties web search:
>
> - **AI agents and automations with no web access — the biggest reason it exists.** This
>   is most programmatic MCP usage: CI jobs, air-gapped ops, headless agents. With no web,
>   the model answers from training data — and in our tests that model was *flatly wrong*
>   on real questions (it denied PXC has a Clone SST method; it recommended `mongosync` and
>   missed Percona's own migration tool). A no-web agent has no way to recover from that.
>   One tool call gives it the current, cited answer. **This is the clear win.**
> - **Freshness on just-released facts.** The corpus is re-ingested daily, so post-cutoff
>   releases are in it — e.g. Percona ClusterSync for MongoDB 0.8.1 (2026-05-07) and the
>   PXC Clone SST method (8.4.4-4, 2025-04-16). A model with no web cannot know these at
>   all; even a web chat can lag the docs.
> - **Deterministic, Percona-scoped retrieval that never invents a URL.** Every hit is a
>   real chunk with a verified live `docs.percona.com` / `forums.percona.com` URL and a
>   relevance score, ranked against a Percona-only corpus. Web ranking gets there too, but
>   not deterministically and not scoped — it can drift into upstream MySQL or another
>   vendor.
> - **Version-scoped retrieval.** `version="8.4"` returns only 8.4-tagged chunks and
>   resolves URLs to the right `/8.4/` path. This is a precision aid — it matters most for
>   weaker models and no-web agents; a strong web chat often already knows, say, that
>   `innodb_log_file_size` is gone in 8.4.
> - **Weaker tool-calling models.** Llama/Qwen/Mistral-class models carry more of the stale
>   defaults (mysqldump for big DBs, `innobackupex`, blocking `ALTER`); the connector — and
>   the companion skills — correct those. A frontier model already avoids most of them.
> - **Day-to-day "how do I configure X".** With web on, a chat usually gets there.
>   **percona-dk ties** (cited inline, less drift) — not transformative. Honest tie.
>
> **Where it helps most, concretely:** any agent writing install scripts, Ansible/Terraform,
> or runbooks *without* web access — the output goes to real infrastructure, and "plausible
> but wrong" is expensive. percona-dk makes the starting point current and cited. A human
> still reviews and runs it.
>
> ### Proof — examples, each verified against the live corpus
>
> 🔵 *wins for no-web agents (most automation)* · 🟡 *ties a web chat*
>
> 1. **🔵 Vanilla denies a real feature — PXC Clone SST.** With no web, this model answers
>    *"PXC does not use the Clone plugin as an SST method"* — wrong. percona-dk returns the
>    [PXC 8.4 Clone SST page](https://docs.percona.com/percona-xtradb-cluster/8.4/clone-sst/)
>    (introduced 8.4.4-4, [PXC-4469](https://perconadev.atlassian.net/browse/PXC-4469)) and
>    notes it's also a tech-preview in 8.0 (default in `wsrep_sst_allowed_methods` since
>    8.0.41). *A web chat also finds this — the win is over the no-web agent.*
> 2. **🔵 Vanilla misses Percona's own tool — Atlas → PSMDB.** With no web, this model
>    recommends MongoDB's `mongosync` and says Percona has no dedicated tool. percona-dk
>    surfaces [Percona ClusterSync for MongoDB (PCSM)](https://docs.percona.com/pcsm/latest/)
>    (formerly Percona Link for MongoDB; current 0.8.1) for change-stream replication. Note
>    the [forum guideline](https://forums.percona.com/t/guideline-for-migrating-from-atlas-cluster-to-percona-mongodb/36958)
>    flags that *truly* zero-downtime Atlas exits are constrained because Atlas doesn't
>    expose backend nodes — plan for minimal, not zero, downtime.
> 3. **🟡 Forum pinpoint, deterministically.** For *"PMM QAN shows no MongoDB queries
>    despite profiling on,"* percona-dk returns the
>    [resolved thread](https://forums.percona.com/t/pmm-query-analytics-for-mongodb-not-showing-any-querys/10577)
>    (accepted answer) plus the
>    [PMM profiling doc](https://docs.percona.com/pmm-doc/latest/setting-up/client/mongodb/),
>    with verified live URLs and no invented links. A web chat finds these too — the
>    connector's edge is determinism and never hallucinating a dead URL.
> 4. **🟡 Pinpoint a blog post among ~280.** *"Recent Percona blog on the MySQL variables
>    that actually matter"* →
>    [the 2026-02-01 post](https://percona.community/blog/2026/02/01/tuning-mysql-for-performance-the-variables-that-actually-matter/)
>    (section 1: `innodb_buffer_pool_size`). Web gets here too; percona-dk is faster and
>    cites a verified URL.
>
> **Verify it in 60 seconds:** ask these with the connector **off** *and with web search
> off* (the real condition for most agents), then **on**. The gap is widest exactly where
> it matters: an agent with no web acting on the answer.
