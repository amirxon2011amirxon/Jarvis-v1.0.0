# Web Research

## Objective

Answer a research question with evidence that can be checked. A good research
result states what is known, how confidently it is known, where sources
disagree, and what remains uncertain. It is not a list of links with a
confident-sounding summary on top.

## When to use this Skill

Use it when:

- the user asks for a comparison, market scan, literature overview, current
  state of a field or a decision based on external evidence;
- facts may have changed recently and must be verified against current sources;
- multiple claims need reconciliation or a recommendation needs justification;
- the user asks for sources, citations or a research report.

Do not use it for:

- a simple definition or explanation you can answer reliably from general
  knowledge, unless current facts matter;
- opening a URL the user already supplied and summarising it — that is a normal
  read, though the source-quality rules below still apply;
- inventing citations to make an answer look researched.

## 1. Define the question

Write the research brief before searching:

- **Question:** one sentence, specific enough to be answerable.
- **Decision or use:** what will the answer be used for?
- **Scope:** time period, geography, product category, population or technical
  constraints.
- **Required freshness:** does the answer depend on this week, this year or
  historical facts?
- **Output:** short answer, comparison table, memo, annotated bibliography or
  recommendation.

If the user's request is broad, state the interpretation you are using and
proceed. Ask a clarifying question only when a wrong interpretation would waste
significant work or produce a misleading answer.

## 2. Plan the search

Use several query angles rather than one phrase:

- the exact entity or concept;
- the comparison terms the user used;
- technical or official terminology;
- known alternatives, vendors or authors;
- sceptical queries ("limitations", "benchmark", "criticism", "failure");
- date-restricted queries for fast-moving topics.

Search breadth should match the claim's importance. A passing factual note needs
less evidence than a purchase recommendation or a safety claim.

## 3. Source hierarchy

Rank sources by proximity to primary evidence:

1. **Primary sources:** official documentation, standards, statutes, peer-reviewed
   papers, raw datasets, company filings, product changelogs, direct
   measurements.
2. **Reputable secondary sources:** established newsrooms, industry analysts with
   disclosed methodology, expert reviews that test the product.
3. **Tertiary/aggregated sources:** encyclopedias, listicles, SEO comparison
   sites, content farms, unattributed summaries.

A primary source can still be wrong or self-interested. A secondary source can
be excellent. Judge the specific claim, not just the domain.

For every important source, note:

- author or organisation;
- publication and last-updated date;
- whether the claim is original reporting, measurement, opinion or repetition;
- funding, commercial interest or advocacy that could bias it;
- whether the methodology or data is visible.

## 4. Verify before synthesising

- **Triangulate.** Confirm material claims with at least two independent
  sources. Independence means they did not copy the same press release or
  dataset.
- **Follow the claim to its origin.** A dozen articles citing one blog post are
  one source, not twelve.
- **Check dates.** A benchmark from three versions ago may not describe the
  current product. Distinguish "published recently" from "about the current
  version".
- **Check units and definitions.** "Users", "active users", "seats", "requests"
  and "tokens" are not interchangeable.
- **Separate fact, estimate and forecast.** Label projections and vendor claims.
- **Look for disconfirming evidence.** Search for failures, limitations and
  independent tests, not only reasons the leading option is good.
- **Quote precisely.** Preserve numbers, qualifiers and scope. Do not turn "in
  one small study" into "research shows".

If a claim cannot be verified, say so and either omit it or mark it clearly as
unverified.

## 5. Handle contradictions

When sources disagree:

1. State the disagreement plainly.
2. Identify the likely reason: different dates, versions, definitions, samples,
   methodologies, regions or incentives.
3. Prefer the source with the stronger method and closer access to primary data.
4. If the conflict cannot be resolved, present both positions and explain what
   evidence would settle it.
5. Do not average incompatible numbers to produce a tidy but false midpoint.

## 6. Synthesise for the decision

Organise the answer around the user's question, not around the order in which
you found sources.

- Lead with the direct answer or recommendation.
- Give the two or three findings that actually change the decision.
- Use a table when comparing consistent attributes across options.
- Explain trade-offs and conditions: "X is faster for small batches; Y is cheaper
  at scale, but only above roughly Z volume."
- Distinguish what is well established from what is emerging or contested.
- Include a short "what I could not verify" section when it matters.

Avoid:

- a wall of undifferentiated bullet points;
- repeating the same claim from five sources;
- confident language unsupported by the cited evidence;
- hiding uncertainty in a footnote;
- recommending an option solely because it has the most search results.

## 7. Citations

- Cite the specific page that supports the claim, not a homepage or search
  result.
- Include title, publisher/author, date when available, and URL.
- Place the citation next to the claim it supports.
- Use direct quotations sparingly and keep them exact.
- Never cite a source you did not actually read.
- If a source is paywalled or inaccessible, say what you could see (abstract,
  snippet) and do not imply you read the full text.
- Prefer stable links; note when a page is dynamic or likely to change.
- Do not fabricate page numbers, publication dates or author names.

## 8. Report structure

A practical research report:

1. **Answer / recommendation** — one short paragraph.
2. **Key findings** — three to six evidence-backed points.
3. **Comparison or detail** — table or structured analysis.
4. **Contradictions and uncertainty** — what sources disagree on and why.
5. **Sources** — numbered list with dates and URLs.
6. **Method note** — only when useful: search scope, date cutoff, inclusion
   criteria.

For a conversational answer, compress this to the answer, the decisive evidence
and two or three citations. Do not deliver a formal report when the user asked a
simple question.

## Tool guidance

- Use `web_fetch` to search and read pages. Read the actual source, not only the
  search-result snippet.
- Use `open_web` only when the user asks to view a page in their browser or when
  the task explicitly requires it. Research does not need to open tabs.
- Record source URLs as you go; do not reconstruct citations from memory.
- If a fetch fails, report the failure and try an alternative source rather than
  inventing content.

## Quality bar

Before delivering, ask:

- Does the answer directly address the question?
- Is every material claim supported by a source I actually read?
- Are dates and versions current enough for the claim?
- Did I look for evidence against my conclusion?
- Are disagreements and unknowns visible?
- Could the user reproduce the search and check the citations?
- Did I avoid presenting vendor marketing as independent evidence?
