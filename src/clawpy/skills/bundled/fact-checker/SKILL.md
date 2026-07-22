---
name: fact-checker
description: Multi-source claim triangulation — verifies factual claims by checking ≥3 independent sources. Uses WebFetch + WebSearch to find evidence and counter-evidence. No single-source truths.
user-invocable: true
---

# Fact Checker

Verify factual claims extracted from news sources using multi-source triangulation.

## Core Principle

**No single-source truths.** A claim is "verified" ONLY if ≥3 independent sources confirm it.

## Verification Process

For each claim:

### Step 1: Classify the Claim
- **Verifiable**: Contains specific facts (numbers, dates, quotes, events)
- **Partially verifiable**: Mix of fact and opinion
- **Opinion**: Not fact-checkable — label and skip

### Step 2: Search for Evidence
Use WebFetch and WebSearch to find:
- **Supporting evidence**: Other sources confirming the same fact
- **Counter-evidence**: Sources disputing or contradicting the claim
- **Primary sources**: Government data, court filings, official statements

Priority of sources:
1. Government/official (.gov, court records, SEC filings)
2. Wire services (AP, Reuters, AFP)
3. Data providers (BLS, Census, CBO)
4. Major newspapers of record
5. Academic/research institutions

### Step 3: Assign Verdict

| Verdict | Criteria |
|---------|----------|
| **Verified** | ≥3 independent sources confirm. Primary source evidence found. |
| **Mostly True** | Core claim accurate but missing nuance or context. |
| **Contested** | ≥2 credible sources present conflicting information. |
| **Misleading** | Technically true but framed to create false impression. |
| **Unverified** | Insufficient evidence to confirm or deny. <3 sources. |
| **False** | Primary source evidence directly contradicts the claim. |

### Step 4: Document Evidence Chain

For each verdict, provide:
- The exact claim text
- Supporting sources with URLs
- Counter-evidence with URLs
- Missing information that would change the verdict
- Confidence level (0.0 - 1.0)

## Output Format

```json
{
  "claims_checked": 5,
  "results": [
    {
      "claim": "Tariff collections have reached $500 billion since 2025",
      "source": "Fox News",
      "verdict": "Mostly True",
      "confidence": 0.8,
      "evidence": [
        {"source": "Treasury.gov", "url": "https://...", "finding": "Collections totaled $487B through Q2 2026"},
        {"source": "Reuters", "url": "https://...", "finding": "Reports $490B in total collections"},
        {"source": "CBO", "url": "https://...", "finding": "CBO estimates $475-510B range"}
      ],
      "counter_evidence": [],
      "missing": "Figure is gross collections — net impact after retaliatory tariffs is lower",
      "note": "Claim is directionally correct but rounds up. 'Reached' vs 'approaching' is the nuance."
    }
  ]
}
```

## Rules

- NEVER fabricate sources or URLs
- If you can't find evidence, say "Unverified" — don't guess
- Check the CLAIM, not whether you agree with the FRAMING
- Wire services (AP, Reuters) are the gold standard for fact baseline
- Government primary sources (.gov) trump all secondary reporting
