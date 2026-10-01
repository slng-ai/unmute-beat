# Testing Wasla by voice

Wasla is Lina from Wasla, calling about one overdue payment. You play the person who picks up.

## How to start

```bash
cd use-cases/wasla
uv run bot.py                      # calls about WA-1001, Layla Haddad
ACCOUNT_ID=WA-1002 uv run bot.py   # or another seeded account
```

Open http://localhost:7860 and press Start.

The seeded accounts are invented:

| `ACCOUNT_ID` | Holder | Date of birth | Amount | Shop |
|---|---|---|---|---|
| WA-1001 | Layla Haddad | 14 March 1990 | 434.50 dirhams | Falcon Electronics |
| WA-1002 | Omar Khalil | 2 November 1985 | 1,249.00 dirhams | Oasis Furniture |
| WA-1003 | Sara Nasser | 30 July 1998 | 89.75 dirhams | Saffron Fashion |

The ledger lives in memory. Restart `bot.py` to reset it, for example after an account is locked.

In the tables below, **code** lines are fixed and should match word for word. **Model** lines are the
model's own words, so only their meaning should match.

## Case 1: the holder promises to pay

| You say | Lina should say | Who |
|---|---|---|
| (wait) | "Hello, this is Lina calling from Wasla. Am I speaking with Layla Haddad?" | code |
| "Yes, speaking." | asks for your date of birth | model |
| "Fourteenth of March, nineteen ninety." | "Is your date of birth the 14th of March, 1990?" | code |
| "Yes." | "Thank you, that matches. Is now a good time to talk about your Wasla account?" | code |
| "Sure." | the recording notice, then "four hundred thirty-four dirhams and fifty fils ... Falcon Electronics ... Could you make the payment today?" | code |
| "Not today. Can I pay next Friday?" | "Shall I record your promise to pay ... on Friday the ...?" | code |
| "Yes." | "Thank you. Your promise to pay on Friday ... is recorded. You can pay in the Wasla app or on our website. ..." | code |
| "No, that's all, bye." | a goodbye, then the call ends | code |

Check:
1. The amount and the shop are never said before your date of birth is confirmed.
2. The promise is read back before it is recorded.

## Case 2: a day too late, then a callback

| You say | Lina should say | Who |
|---|---|---|
| (wait), "Yes.", your date of birth, "Yes." | as in case 1, up to "Is now a good time ...?" | code |
| "Sure." | the notice and the amount | code |
| "I can pay in three weeks." | "I can only note a day up to <ten days from today>. Which day by then works for you?" | code |
| "Actually, can you call me back tomorrow at 5?" | goes to the callback step and reads the callback back | code |
| "Yes." | the callback is confirmed | code |

Check:
1. A day more than ten days away is refused. Nothing is recorded for it.
2. Say "no" to any read-back once. Lina should ask what to change, and record nothing.

## Case 3: the wrong person answers

Run this case twice, once each way.

**3a. A wrong number.**

| You say | Lina should say | Who |
|---|---|---|
| (wait) | "... Am I speaking with Layla Haddad?" | code |
| "No, there's no Layla here." | "I'm sorry to have bothered you. Shall I take this number off our list, so we don't call it again?" | code |
| "Yes please." | "Done. You won't get any more calls from us on this number. Sorry for the trouble." | code |

**3b. Someone else who knows the holder.**

| You say | Lina should say | Who |
|---|---|---|
| "This is her husband, she's out." | "When would be a good day and time to call back?" | code |
| "What is this about? How much does she owe?" | refuses kindly: it can only discuss it with Layla | model |

Check:
1. In 3b, Lina never says the amount, the shop or the dates.
2. Give a wrong date of birth three times in a row. The third time, Lina should say she can't go on today.
   After that, even the right date is refused until you restart `bot.py`.
