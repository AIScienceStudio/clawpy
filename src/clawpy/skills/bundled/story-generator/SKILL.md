---
name: story-generator
description: Generate a narrative intelligence story page from contested news topics — discovers framing splits across Left/Center/Right sources.
user-invocable: true
---

# Story Generator Skill

Generate a Perspectivity narrative story page for a contested news topic.

## Process

1. **Topic analysis**: Take the user's topic and search for diverse coverage.
   Use WebFetch to find how Left, Center, and Right outlets frame the story.

2. **Source gathering**: Find 4-6 sources covering the topic from different
   political positions. Prioritize:
   - Left: CNN, MSNBC, NYT, WashPost, Vox, HuffPost
   - Center: AP, Reuters, BBC, PBS, NPR, The Hill
   - Right: Fox News, Daily Wire, National Review, WSJ Opinion, NY Post

3. **Framing analysis**: For each source, identify:
   - FRAME: Rhetorical devices used (loaded language, false equivalence, etc.)
   - CLAIM: Key factual assertions made
   - VALUE: Contestable judgments or editorial positions
   - OMISSION: What this source leaves out that others cover

4. **Synthesis**: Produce:
   - Hook: One paragraph describing the 2-3 competing framings
   - Framing split: What Left/Center/Right each emphasize
   - Omission map: What each side leaves out
   - Contradictions: Where sources directly conflict on facts
   - Claim board: Key claims with verdict (verified/contested/unverified)

## Output Format

Return a JSON object with this structure:
```json
{
  "title": "Story title",
  "hook": "One paragraph hook describing the framing split",
  "spectrum": {"left": 2, "center": 2, "right": 2},
  "framing_split": {
    "left": [{"device": "...", "quote": "...", "outlet": "..."}],
    "center": [...],
    "right": [...]
  },
  "omission_map": [
    {"side": "left", "omitted": ["..."], "covered_by": ["right"]}
  ],
  "contradictions": [
    {"topic": "...", "left": "...", "right": "..."}
  ],
  "claims": [
    {"text": "...", "verdict": "verified|contested|unverified", "evidence": ["..."]}
  ],
  "sources": [
    {"outlet": "...", "url": "...", "lean": "left|center|right"}
  ]
}
```
