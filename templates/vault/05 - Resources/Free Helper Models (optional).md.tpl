---
status: active
type: reference
---
# Free Helper Models (optional, you set this up yourself)

This kit does not install any helper models. This note is the experience behind them, so you can decide whether to set them up and how to use them safely.

## What it is
A local gateway program (we used OmniRoute) runs on your own computer at a local address and routes requests to the free tiers of several AI providers behind one door. {{assistant_name}} (the boss, the model you pay for) hands small, well-described coding and writing jobs to those cheaper helpers, then checks what comes back. It saves your paid plan for the thinking and the checking.

## Why bother
Your paid plan has a weekly limit. In our setup the limit was the thing that ran out first, long before ideas did. Helpers wrote first drafts of tests, small modules and reports for free; the boss reviewed them. It only pays off when checking a draft costs less than writing the code yourself.

## What you have to do yourself
- Install the gateway from its own instructions and connect providers with accounts that are yours. Use a throwaway email for free-tier signups, not your main one.
- Keep every key and password in the Windows Credential Manager (or your system's keychain), never in a file the assistant can read or in a note.
- Make two or three named chains, for example one fast, one smarter, and one that falls back when a provider is rate-limited. Free tiers have daily caps; expect some jobs to fail and be retried.
- Write a tiny runner script that sends one brief to a chain and saves the answer in a job folder. Ask {{assistant_name}} to write it with you once the gateway runs.

## Rules that kept it safe and useful
1. **Nothing private goes to a free model.** Prompts leave your computer for free online providers. Scan every brief for emails, ids, keys and personal notes before it is sent, and refuse if anything matches.
2. **Narrow briefs.** One function or one test file per job, exact cases listed, "do not edit the module", the command to run. Retry a failed job with a narrower brief before taking over.
3. **Review everything.** Read the deliverable in full, run its tests, then break the code on purpose in a few small ways and confirm the tests fail. In our setup every helper deliverable needed at least one fix (a dead entry left behind by a cancel, a test that hard-coded a path on the helper's machine, tests that never failed).
4. **Do not let a helper's test write to your real logs.** Any test that calls `main()` must point the log folder at a temporary folder first.
5. **One background command per job.** Never a shell `&`; nothing wakes the session when it finishes.
6. **Save results at once** and note which helper produced what. If a job fails twice, do it yourself and say so.
7. **Cost cap on checking.** Do not spend more attention checking a draft than writing it would take.

## Honest numbers from our setup
Helpers wrote the first draft of dozens of tests, a few small modules and some reports. They were fast on mechanical work and unreliable on anything that needed judgment about your files. Treat them as interns whose work always gets checked.
