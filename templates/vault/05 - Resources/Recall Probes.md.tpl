---
status: active
type: reference
---
# Recall Probes

Questions the memory must be able to answer. `scripts/recall_eval.py` searches the vault for each question and checks that the expected text comes back in the top 5 lines. When you add an important fact to the notes, add a probe for it here, one line each, in the form `Q: question | A: expected text`. A score under 80 percent means the notes or the search need work.

Q: What is the owner's name? | A: {{name}}
Q: What is the assistant called? | A: {{assistant_name}}
Q: When are the quiet hours? | A: {{quiet_hours}}
Q: What does the owner want most right now? | A: {{goals}}
Q: What tools does the owner already use? | A: {{tools}}
