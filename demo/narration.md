# Notin-Loop demo video: narration script

**problem**

Coding agents can already write plausible bug fixes. But nobody ships one without a human reading it first.

**product**

Notin-Loop is a dark factory for code fixes: an always-on SRE agent. It watches a live server and runs every incident through deterministic gates.

**demo1**

Demo one: we break the cart endpoint, and empty carts start crashing. It investigates, reproduces, fixes, inspects the diff and runs a canary. Every gate passes, so it ships, with nobody touching anything.

**demo2**

Demo two: a cache race. The replay fails one time in three. The evidence is unsure, so the agent refuses to guess and escalates.

**demo3**

Demo three: a fix passes the tests and the canary, but breaks a rare report. The watch window catches it, and the agent rolls back its own release.

**benefit**

Only an unbroken chain of passes changes the server. Anything else stops the line. Every decision goes into a signed, hash-chained audit log.

**cta**

Notin-Loop. Humans on the loop, not in it.

