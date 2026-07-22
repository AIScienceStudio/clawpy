---
name: web-research
description: Deep web research — search multiple sources, verify claims, synthesize a cited report.
user-invocable: true
---

# Web Research Skill

When invoked, perform deep multi-source web research on the given topic.

## Process

1. **Search broadly**: Use WebFetch to access 3-5 authoritative sources on the topic.
   Prioritize: official sources (.gov), wire services (AP, Reuters), major newspapers.

2. **Extract key claims**: From each source, identify the main factual claims,
   data points, and positions stated.

3. **Cross-reference**: Check if claims from one source are confirmed, contradicted,
   or absent in other sources. Flag conflicts.

4. **Synthesize**: Write a concise research report with:
   - Summary (2-3 sentences)
   - Key findings (bulleted, with source attribution)
   - Conflicting claims (if any, with both sides cited)
   - Sources consulted (title + URL)

## Output Format

Return the report in clean markdown. Cite sources inline with [Source Name](URL).
Do not speculate beyond what sources state. Mark unverified claims as such.
