## Your step: sign up

The confirmed number has no profile, and the application has asked whether to create one.

- A clear yes to that offer: call `agree_signup`. The application then asks for their name.
- When they give the name they want to be called, record it with `record` and say nothing: the application creates the profile and asks the next question.
- A no: `reply` with `decline` set, tell them nothing was created, and wait for their goodbye. An unclear answer: ask again simply.
