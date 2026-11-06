---
name: verify-before-claiming
description: "Use before saying that something is done, fixed, saved, sent, working, current or counted; before closing a work item; before a final summary of a multi-step change."
---

# Verify before claiming

A claim is only as good as the evidence gathered in the same message that makes it.

1. **Name the claim** in one sentence ("the tests pass", "the note is saved", "the message was sent").
2. **Get the evidence now:** run the command, read the file back, read the log line. Not from memory, not from an earlier turn, not from a helper's report.
3. **Check it shows the claimed result.** A command that exited 0 only shows it ran. "Sent" needs the sent line, not "queued". "Saved" needs the file read back. A count needs the count command run again.
4. **Say what you did not check.** A sample is a sample; a part is a part. Write "I checked X, not Y".
5. **Big deliverables:** re-read the request, list its items, tick each against the result. After a context reset, treat the summary's "done" as a claim to re-verify.
6. **Three failed fixes for one bug means stop:** question your model of the problem and trace the true origin; no fourth patch.
7. **When unsure, say so** plainly instead of rounding up to "done".

For work done by a helper model or another agent, give a separate reviewer the files and the tool log, not your summary (`claim-auditor` agent), and break the code on purpose to see the tests fail.
