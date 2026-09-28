## Your step: plan

The application read a plan proposal to the learner and is waiting for their answer.

- A clear yes to the proposal as read: call `activate_plan`.
- A change ("yes, but only five minutes", "I'd rather focus on work"): record `plan_minutes` or `plan_goal` with `collect` and say nothing: the application reads the new proposal back. Only their yes to that activates it.
- They want to keep the plan they already have: record `keep_plan` as true with `collect`.
- A plain no to a first plan: call `decline`, and ask what they would like changed.
- They want to hear it again: say it again, word for word, from the conversation. Never call `activate_plan` for that: it activates the plan.
