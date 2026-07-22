---
name: framing-analyzer
description: Per-source three-axis framing analysis — extracts FRAMES (rhetorical devices), CLAIMS (factual assertions), and VALUES (contestable judgments) from news articles. The backbone of Perspectivity's narrative intelligence.
user-invocable: true
---

# Framing Analyzer

Analyze a single news source (article or transcript) through three lenses:

## Three-Axis Analysis

### 1. FRAME — Rhetorical Construction
Identify rhetorical devices used to shape perception:

| Device | Example |
|--------|---------|
| `loaded_language` | "radical", "common-sense", "extremist", "hero" |
| `false_equivalence` | Treating unequal positions as equally valid |
| `fear_appeal` | "threatens our way of life", "crisis" |
| `authority_appeal` | "experts say", "studies show" (without citation) |
| `whataboutism` | Deflecting criticism by pointing to another issue |
| `cherry_picking` | Using specific stats while ignoring contradicting ones |
| `straw_man` | Misrepresenting opponent's position to attack it |
| `appeal_to_emotion` | Human interest stories used to drive policy conclusions |

For each frame: quote the exact text, classify the device, rate severity (low/medium/high).

### 2. CLAIM — Factual Assertions
Extract every factual claim made:

- **Verifiable**: Can be checked against records/data (e.g., "$500 billion in tariffs collected")
- **Partially verifiable**: Contains verifiable and opinion elements mixed
- **Opinion**: Judgment or prediction not checkable against data

For each claim: extract exact text, classify verifiability, note evidence cited (or missing).

### 3. VALUE — Contestable Judgments
Identify value-laden statements that reasonable people could disagree on:

- Policy preferences ("we should...")
- Moral judgments ("it's wrong to...")
- Priority claims ("the most important thing is...")

Label but do NOT adjudicate. Maintain neutral tone.

## Overlap Cells (Critical for Quality)

The most important findings are where CLAIM and FRAME overlap:
- A factual claim wrapped in loaded framing
- A cherry-picked stat used to support a straw man
- An emotional appeal that contains a verifiable claim

Example: "CNN framed the tariffs as a 'reckless trade war' (FRAME: loaded_language) while reporting '$500B collected' (CLAIM: verifiable). The framing device colors the factual claim."

## Output Format

```json
{
  "source": "CNN",
  "outlet_lean": "LC",
  "observations": [
    {
      "type": "FRAME",
      "device": "loaded_language",
      "quote": "reckless trade war that hurts American families",
      "severity": "medium",
      "context": "Lead paragraph framing of tariff policy"
    },
    {
      "type": "CLAIM",
      "text": "Tariff collections have reached $500 billion since 2025",
      "verifiability": "verifiable",
      "evidence_cited": "Treasury Department data",
      "verdict": "unverified"
    },
    {
      "type": "VALUE",
      "text": "Free trade is essential for American prosperity",
      "label": "economic_philosophy"
    }
  ],
  "omissions": [
    "Does not mention job creation in protected industries",
    "No consumer price impact data cited"
  ],
  "overlap_cells": [
    {
      "frame": "loaded_language",
      "claim": "Tariff collections $500B",
      "analysis": "The factual claim is embedded in emotionally charged framing, making it harder to evaluate objectively"
    }
  ]
}
```

## Rules

- Quote exact text, don't paraphrase
- Classify don't adjudicate — your job is to IDENTIFY framing, not judge it
- Every observation must cite the specific text that triggered it
- Omissions are what this source DOESN'T say that other sources DO say
- Be equally rigorous on Left, Center, and Right sources
